"""作业管控服务规则测试。复用 demo.build 注册的线路、人员与风险票。"""

from __future__ import annotations

import contextlib
import io
import unittest
from datetime import datetime, timedelta

from src.grid_maintenance import errors as err
from src.grid_maintenance.demo import build, run as run_demo
from src.grid_maintenance.model import (
    EvidenceKind,
    Line,
    Person,
    Qualification,
    RiskTicket,
    Role,
    Section,
    Tower,
    Verdict,
    WindowState,
)
from src.grid_maintenance.tracing import build_overview, render_overview


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build()
        self.t = self.svc.now

    def at(self, hours: float) -> datetime:
        return self.t + timedelta(hours=hours)

    def isolate_window(self, window_id: str = "W-1", tickets=("TK-1",)) -> None:
        """排程→调度停电→现场确认隔离，三步走到可作业状态。"""
        self.svc.allocate_window(
            command_id="win", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(10), window_id=window_id,
        )
        self.svc.approve_deenergization(
            command_id="off", dispatcher_id="p-dispatch", window_id=window_id
        )
        self.svc.confirm_isolation(
            command_id="iso", actor_id="p-mgr", window_id=window_id, ticket_ids=list(tickets)
        )

    # ---------------------------------------------------------- 窗口与隔离

    def test_window_overlap_within_section_rejected(self):
        self.svc.allocate_window(
            command_id="a", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(5), window_id="W-a",
        )
        with self.assertRaises(err.WindowConflictError):
            self.svc.allocate_window(
                command_id="b", actor_id="p-mgr", section_id="S1",
                planned_start=self.at(4), planned_end=self.at(8), window_id="W-b",
            )
        # 首尾相接不算重叠
        ok = self.svc.allocate_window(
            command_id="c", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(5), planned_end=self.at(8), window_id="W-c",
        )
        self.assertTrue(ok.ok)

    def test_window_bad_range_and_duplicate_id(self):
        with self.assertRaises(err.WindowConflictError):
            self.svc.allocate_window(
                command_id="a", actor_id="p-mgr", section_id="S1",
                planned_start=self.at(5), planned_end=self.at(5), window_id="W-a",
            )
        self.svc.allocate_window(
            command_id="b", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(5), window_id="W-a",
        )
        with self.assertRaises(err.WindowConflictError):
            self.svc.allocate_window(
                command_id="b2", actor_id="p-mgr", section_id="S1",
                planned_start=self.at(6), planned_end=self.at(9), window_id="W-a",
            )

    def test_only_manager_allocates_and_only_dispatcher_deenergizes(self):
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.allocate_window(
                command_id="a", actor_id="p-build", section_id="S1",
                planned_start=self.at(0), planned_end=self.at(5), window_id="W-a",
            )
        self.svc.allocate_window(
            command_id="b", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(5), window_id="W-a",
        )
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.approve_deenergization(
                command_id="c", dispatcher_id="p-mgr", window_id="W-a"
            )

    def test_reschedule_only_before_lock_and_replays(self):
        self.isolate_window()
        with self.assertRaises(err.StateError):
            self.svc.reschedule_window(
                command_id="r", actor_id="p-mgr", window_id="W-1",
                planned_start=self.at(1), planned_end=self.at(11),
            )
        # 新窗口仅排程时可以调一次
        self.svc.allocate_window(
            command_id="w2", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(20), planned_end=self.at(24), window_id="W-2",
        )
        r1 = self.svc.reschedule_window(
            command_id="r2", actor_id="p-mgr", window_id="W-2",
            planned_start=self.at(21), planned_end=self.at(25),
        )
        self.assertEqual(r1.result["revision"], 1)
        r2 = self.svc.reschedule_window(
            command_id="r2-resend", actor_id="p-mgr", window_id="W-2",
            planned_start=self.at(21), planned_end=self.at(25),
        )
        self.assertTrue(r2.replay)
        self.assertEqual(r2.result["revision"], 1)  # 没有第二次修订

    def test_isolation_requires_ticket_in_section(self):
        line2 = Line("L2", "备用线", 220)
        sec2 = Section("S2", "L2", "备用段")
        sec2.towers["T9"] = Tower("T9", "S2", "N9塔")
        line2.sections["S2"] = sec2
        self.svc.register_line(line2)
        self.svc.register_ticket(RiskTicket("TK-2", "S2", "外区段票", ("接地",)))
        self.svc.allocate_window(
            command_id="w", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(5), window_id="W-9",
        )
        self.svc.approve_deenergization(
            command_id="o", dispatcher_id="p-dispatch", window_id="W-9"
        )
        with self.assertRaises(err.WindowConflictError):
            self.svc.confirm_isolation(
                command_id="i", actor_id="p-mgr", window_id="W-9", ticket_ids=["TK-2"]
            )
        with self.assertRaises(err.ClosureError):
            self.svc.confirm_isolation(
                command_id="i2", actor_id="p-mgr", window_id="W-9", ticket_ids=[]
            )

    # ---------------------------------------------------------- 权限与资质

    def _make_op(self, op_id="OP1", team="team-A", work_type="塔基施工",
                 tower="T-N12", device=None, depends=()):
        self.svc.create_operation(
            command_id="mk-" + op_id, actor_id="p-mgr", window_id="W-1",
            title=op_id, work_type=work_type, team_id=team,
            target_tower_id=tower, target_device_id=device,
            op_id=op_id, depends_on=depends,
        )

    def test_other_team_cannot_touch_operation(self):
        self.isolate_window()
        self._make_op()
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.start_operation(command_id="s", actor_id="p-maint", op_id="OP1")

    def test_expired_qualification_blocks_start(self):
        self.svc.register_person(
            Person(
                "p-expired", "过期赵", (Role.WORKER,),
                (Qualification("旧证", "塔基施工", expires_at=self.at(-1)),),
                team_id="team-A",
            )
        )
        self.isolate_window()
        self._make_op()
        with self.assertRaises(err.QualificationError):
            self.svc.start_operation(command_id="s", actor_id="p-expired", op_id="OP1")

    def test_dependency_must_close_first(self):
        self.isolate_window()
        self._make_op("OPA")
        self._make_op("OPB", depends=("OPA",))
        with self.assertRaises(err.DependencyError):
            self.svc.start_operation(command_id="s", actor_id="p-build", op_id="OPB")

    # ---------------------------------------------------------- 缺陷暂停/恢复

    def _chain_three_ops(self):
        """A（team-A 塔基）→ B（team-B 绝缘子，设备 D-ins-N12）→ C（team-D 复核）。"""
        self.isolate_window()
        self._make_op("OPA", team="team-A", work_type="塔基施工")
        self._make_op("OPB", team="team-B", work_type="绝缘子更换", device="D-ins-N12",
                      depends=("OPA",))
        self.svc.create_operation(
            command_id="mk-OPC", actor_id="p-mgr", window_id="W-1",
            title="OPC", work_type="无人机复核", team_id="team-D",
            target_tower_id="T-N12", target_device_id="D-ins-N12",
            op_id="OPC", depends_on=("OPB",),
        )
        self.svc.start_operation(command_id="sa", actor_id="p-build", op_id="OPA")
        self.svc.complete_operation(command_id="ca", actor_id="p-build", op_id="OPA")
        self.svc.start_operation(command_id="sb", actor_id="p-maint", op_id="OPB")

    def test_new_defect_pauses_downstream_but_keeps_prior_ndt(self):
        self._chain_three_ops()
        # B 在进行中先有一份探伤类合格证据
        self.svc.record_evidence(
            command_id="xray", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.XRAY, verdict=Verdict.PASS, detail="焊缝正常",
        )
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        self.assertEqual(self.svc.operations["OPB"].state.value, "已闭环")
        # 之后人工检查发现缺陷：B 退回已完成，C 暂停；探伤结论必须还在
        r = self.svc.record_evidence(
            command_id="man", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="销钉缺失",
        )
        defect_id = r.result["defect_id"]
        self.assertIsNotNone(defect_id)
        self.assertEqual(self.svc.operations["OPC"].state.value, "暂停")
        self.assertEqual(self.svc.operations["OPB"].state.value, "已完成")
        xray = [e for e in self.svc.evidences.values() if e.kind == EvidenceKind.XRAY]
        self.assertEqual(len(xray), 1)
        self.assertEqual(xray[0].verdict, Verdict.PASS)

        # 缺陷由本队整改、他人验收后，B 闭环、C 恢复
        self.svc.rectify_defect(
            command_id="fix", actor_id="p-accept", defect_id=defect_id, note="补销钉"
        )
        self.svc.accept_defect(
            command_id="acc", acceptor_id="p-feng", defect_id=defect_id, accepted=True
        )
        self.assertEqual(self.svc.operations["OPB"].state.value, "已闭环")
        self.assertEqual(self.svc.operations["OPC"].state.value, "待开工")

    # ---------------------------------------------------------- 冲突双证据

    def test_manual_drone_conflict_keeps_both_and_escalates(self):
        self._chain_three_ops()
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        # 人工不合格
        self.svc.record_evidence(
            command_id="m", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="销钉缺失",
        )
        # 无人机合格 → 冲突升级，且不重复立案
        r = self.svc.record_evidence(
            command_id="d", actor_id="p-drone", op_id="OPB",
            kind=EvidenceKind.DRONE, verdict=Verdict.PASS, detail="图像正常",
        )
        conflict_id = r.result["conflict_id"]
        self.assertIsNone(r.result["defect_id"])  # 冲突待复核期间不另立缺陷
        self.assertIsNotNone(conflict_id)
        device_evs = [e for e in self.svc.evidences.values() if e.device_id == "D-ins-N12"]
        self.assertEqual(len(device_evs), 2)  # 两份证据都保留

        # 冲突未判定 → 不能送电
        with self.assertRaises(err.ClosureError):
            self.svc.approve_restoration(
                command_id="go", dispatcher_id="p-dispatch", window_id="W-1"
            )
        # 原检查人不能自己复核
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.resolve_conflict(
                command_id="rev", actor_id="p-maint", conflict_id=conflict_id,
                verdict=Verdict.FAIL, resolution="自检",
            )
        # 第三方复核不合格：第三份证据落库，维持/补登缺陷
        rr = self.svc.resolve_conflict(
            command_id="rev2", actor_id="p-review", conflict_id=conflict_id,
            verdict=Verdict.FAIL, resolution="确认销钉缺失",
        )
        self.assertEqual(len(self.svc.evidences), 3)
        self.assertTrue(
            all(e.device_id == "D-ins-N12" for e in self.svc.evidences.values())
        )
        review = self.svc.evidences[rr.result["review_evidence_id"]]
        self.assertEqual(review.kind, EvidenceKind.REVIEW)
        open_defects = [
            d for d in self.svc.defects.values()
            if d.device_id == "D-ins-N12" and d.state.value != "验收通过已闭环"
            and d.state.value != "复核改判销缺"
        ]
        self.assertEqual(len(open_defects), 1)

    def test_conflict_review_pass_dismisses_and_closes(self):
        self._chain_three_ops()
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        # 人工合格、无人机不合格 → 冲突
        self.svc.record_evidence(
            command_id="m", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.PASS, detail="目视正常",
        )
        r = self.svc.record_evidence(
            command_id="d", actor_id="p-drone", op_id="OPB",
            kind=EvidenceKind.DRONE, verdict=Verdict.FAIL, detail="疑似放电痕",
        )
        conflict_id = r.result["conflict_id"]
        # 复核合格：原两份证据仍在，作业可闭环
        self.svc.resolve_conflict(
            command_id="rev", actor_id="p-review", conflict_id=conflict_id,
            verdict=Verdict.PASS, resolution="复查为污渍，无放电痕迹",
        )
        self.assertEqual(self.svc.operations["OPB"].state.value, "已闭环")
        self.assertEqual(
            len([e for e in self.svc.evidences.values() if e.device_id == "D-ins-N12"]),
            3,
        )

    # ---------------------------------------------------------- 验收利益回避

    def test_cannot_accept_own_rectification(self):
        self._chain_three_ops()
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        r = self.svc.record_evidence(
            command_id="m", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="缺销钉",
        )
        defect_id = r.result["defect_id"]
        self.svc.rectify_defect(
            command_id="fix", actor_id="p-accept", defect_id=defect_id, note="已补"
        )
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.accept_defect(
                command_id="acc", acceptor_id="p-accept",
                defect_id=defect_id, accepted=True,
            )
        # 无验收角色的人不能验收
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.accept_defect(
                command_id="acc2", acceptor_id="p-build",
                defect_id=defect_id, accepted=True,
            )
        accepted = self.svc.accept_defect(
            command_id="acc3", acceptor_id="p-feng",
            defect_id=defect_id, accepted=True, remark="合格",
        )
        self.assertTrue(accepted.ok)

    def test_rejected_rectification_can_be_redone(self):
        self._chain_three_ops()
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        r = self.svc.record_evidence(
            command_id="m", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="螺栓松",
        )
        did = r.result["defect_id"]
        self.svc.rectify_defect(command_id="f1", actor_id="p-accept",
                                defect_id=did, note="紧过一次")
        rej = self.svc.accept_defect(
            command_id="a1", acceptor_id="p-feng", defect_id=did,
            accepted=False, remark="力矩仍不足",
        )
        self.assertEqual(rej.result["state"], "验收驳回")
        # 历史整改记录保留，可再次整改
        self.svc.rectify_defect(command_id="f2", actor_id="p-accept",
                                defect_id=did, note="按力矩复紧")
        self.svc.accept_defect(command_id="a2", acceptor_id="p-feng",
                               defect_id=did, accepted=True)
        self.assertEqual(self.svc.defects[did].state.value, "验收通过已闭环")
        self.assertEqual(len(self.svc.defects[did].history), 2)

    def test_other_team_cannot_rectify(self):
        self._chain_three_ops()
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        r = self.svc.record_evidence(
            command_id="m", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="缺件",
        )
        did = r.result["defect_id"]
        # 缺陷来自 team-B 的作业，team-A 不能整改
        with self.assertRaises(err.PermissionDeniedError):
            self.svc.rectify_defect(command_id="f", actor_id="p-build",
                                    defect_id=did, note="越界整改")

    # ---------------------------------------------------------- 中途故障

    def test_incident_halts_then_resumes_without_losing_evidence(self):
        self._chain_three_ops()
        self.svc.record_evidence(
            command_id="ev", actor_id="p-maint", op_id="OPB",
            kind=EvidenceKind.MANUAL, verdict=Verdict.PASS, detail="阶段检查合格",
        )
        self.svc.raise_incident(
            command_id="inc", actor_id="p-maint", window_id="W-1",
            description="突发异响",
        )
        self.assertEqual(self.svc.windows["W-1"].state, WindowState.ACTIVE_HALT)
        with self.assertRaises(err.DependencyError):
            self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        # 暂停期间新工序也不能开工
        with self.assertRaises(err.DependencyError):
            self.svc.start_operation(command_id="sc", actor_id="p-drone", op_id="OPC")
        self.svc.resolve_incident(
            command_id="rin", actor_id="p-maint",
            incident_id=next(iter(self.svc.incidents)), resolution="异物清除",
        )
        self.assertEqual(self.svc.windows["W-1"].state, WindowState.ISOLATED)
        self.svc.complete_operation(command_id="cb", actor_id="p-maint", op_id="OPB")
        # 故障前的证据原样有效
        self.assertTrue(any(
            e.detail == "阶段检查合格" for e in self.svc.evidences.values()
        ))
        # 已排除的故障：相同回执重发只回放，不重复处理
        again = self.svc.resolve_incident(
            command_id="rin2", actor_id="p-maint",
            incident_id=next(iter(self.svc.incidents)), resolution="异物清除",
        )
        self.assertTrue(again.replay)
        # 同键但内容不同 → 判重放拒绝
        with self.assertRaises(err.ReplayError):
            self.svc.resolve_incident(
                command_id="rin3", actor_id="p-maint",
                incident_id=next(iter(self.svc.incidents)), resolution="再清一次",
            )

    # ---------------------------------------------------------- 送电闸门与幂等

    def test_restoration_blocked_until_everything_closed(self):
        self.isolate_window()
        # 没有任何作业
        with self.assertRaises(err.ClosureError):
            self.svc.approve_restoration(
                command_id="go0", dispatcher_id="p-dispatch", window_id="W-1"
            )
        self._make_op("OPA")
        self.svc.start_operation(command_id="s", actor_id="p-build", op_id="OPA")
        with self.assertRaises(err.ClosureError):
            self.svc.approve_restoration(
                command_id="go1", dispatcher_id="p-dispatch", window_id="W-1"
            )
        self.svc.complete_operation(command_id="c", actor_id="p-build", op_id="OPA")
        ok = self.svc.approve_restoration(
            command_id="go2", dispatcher_id="p-dispatch", window_id="W-1"
        )
        self.assertEqual(ok.result["state"], "已恢复送电")
        self.assertEqual(self.svc.tickets["TK-1"].status.value, "已闭环")
        basis = self.svc.releases["W-1"]
        self.assertEqual(basis.operation_count, 1)
        self.assertEqual(basis.last_command_id, "go2")

    def test_restoration_and_receipts_are_idempotent(self):
        self.isolate_window()
        self._make_op("OPA")
        self.svc.start_operation(command_id="s", actor_id="p-build", op_id="OPA")
        self.svc.complete_operation(command_id="c", actor_id="p-build", op_id="OPA")
        first = self.svc.approve_restoration(
            command_id="go", dispatcher_id="p-dispatch", window_id="W-1"
        )
        restored_at = self.svc.windows["W-1"].restored_at
        # 换命令号重发：回放原回执，不产生第二次送电
        second = self.svc.approve_restoration(
            command_id="go-resend", dispatcher_id="p-dispatch", window_id="W-1"
        )
        self.assertTrue(second.replay)
        self.assertEqual(second.result, first.result)
        self.assertEqual(self.svc.windows["W-1"].restored_at, restored_at)

    def test_same_key_different_payload_is_replay_attack(self):
        self.svc.allocate_window(
            command_id="w", actor_id="p-mgr", section_id="S1",
            planned_start=self.at(0), planned_end=self.at(5), window_id="W-1",
        )
        with self.assertRaises(err.ReplayError):
            # 幂等键含 window_id+start，改结束时间即键同内容不同
            self.svc.allocate_window(
                command_id="w2", actor_id="p-mgr", section_id="S1",
                planned_start=self.at(0), planned_end=self.at(6), window_id="W-1",
            )

    def test_failed_command_does_not_reserve_idempotency_key(self):
        self.isolate_window()
        for cmd in ("fail-1", "fail-2"):
            with self.assertRaises(err.ClosureError):
                self.svc.approve_restoration(
                    command_id=cmd, dispatcher_id="p-dispatch", window_id="W-1"
                )
        self.assertIsNone(self.svc.ledger.lookup("restore:W-1"))

    # ---------------------------------------------------------- 总览追溯

    def test_overview_traces_line_to_release_basis(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run_demo(self.svc)
        ov = build_overview(self.svc, "L-TL1")
        self.assertTrue(ov.ready_to_restore)
        text = render_overview(ov)
        self.assertIn("天林Ⅰ线", text)
        self.assertIn("N12塔", text)
        self.assertIn("X光探伤", text)
        self.assertIn("放行依据", text)
        self.assertIn("整改 吴验收", text)
        self.assertIn("验收人 冯验收", text)
        self.assertIn("升级复核", text)
        self.assertIn("已恢复送电", text)

    def test_overview_reports_first_blocker(self):
        self.isolate_window()
        self._make_op("OPA")
        ov = build_overview(self.svc, "L-TL1")
        self.assertFalse(ov.ready_to_restore)
        self.assertIn("未闭环", ov.blocker)


if __name__ == "__main__":
    unittest.main()
