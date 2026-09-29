"""作业管控服务的端到端与边界测试。"""

import unittest

from src.grid_maintenance.model import (
    DefectState,
    Qualification,
    ReviewState,
    TaskState,
    Verdict,
    WindowState,
    WorkType,
)
from src.grid_maintenance.service import MaintenanceError, MaintenanceService


def build_world() -> MaintenanceService:
    """构造一条线路、一个隔离区段、两基塔、两支队伍与全套角色人员。"""
    svc = MaintenanceService(clock=iter([
        f"2026-10-01T08:{m:02d}:00" for m in range(0, 59)
    ]).__next__)

    svc.register_line("L1", "500千伏天林Ⅰ线")
    svc.register_section("L1", "S1", "1号检修区段", isolation_zone="Z1")
    svc.register_tower("S1", "T1", "N101塔")
    svc.register_tower("S1", "T2", "N102塔")
    svc.register_device("T1", "D1", "T1绝缘子串")
    svc.register_device("T1", "D2", "T1金具")
    svc.register_device("T2", "D3", "T2故障诊断装置位")

    svc.register_crew("C1", "基建一队", {Qualification.CONSTRUCTION})
    svc.register_crew("C2", "运维一班", {Qualification.MAINTENANCE})
    svc.register_crew("C3", "检测中心", {Qualification.INSPECTION})

    svc.register_person("P1", "赵基建", "C1", {Qualification.CONSTRUCTION})
    svc.register_person("P7", "郑杂工", "C1", {Qualification.INSPECTION})
    svc.register_person("P2", "钱运维", "C2", {Qualification.MAINTENANCE})
    # 钱运维同时持有验收资质，用于验证“不得验收自己整改的缺陷”
    svc.people["P2"].qualifications.add(Qualification.ACCEPTANCE)
    svc.register_person("P3", "孙探伤", "C3", {Qualification.INSPECTION})
    svc.register_person("P4", "李无人机", "C3", {Qualification.INSPECTION})
    svc.register_person("P5", "周调度", "C2", {Qualification.DISPATCH})
    svc.register_person("P6", "吴验收", "C2", {Qualification.ACCEPTANCE})
    return svc


ALL_WORKS = {
    WorkType.FOUNDATION,
    WorkType.INSULATOR,
    WorkType.FLAW_DETECTION,
    WorkType.FAULT_DEVICE,
    WorkType.DRONE_REVIEW,
    WorkType.MANUAL_INSPECTION,
}


def plan_window(svc: MaintenanceService) -> str:
    """分配一张全风险票覆盖的窗口（尚未批准）。"""
    svc.add_risk_ticket("R1", "S1", ALL_WORKS, "Z1", approved=True)
    return svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")


def approve_and_lock(svc: MaintenanceService, win: str) -> None:
    """调度员批准停电，现场队伍确认安全隔离并锁定。"""
    svc.approve_outage(win, "P5", "CMD-1")
    svc.confirm_site_lock(win, "P1", "CMD-2")


def standard_tasks(svc: MaintenanceService, win: str) -> dict[str, str]:
    """标准工序链：塔基/探伤 → 绝缘子/故障装置 → 人工精检/无人机复核。"""
    foundation = svc.add_task(win, WorkType.FOUNDATION, "C1", tower_id="T1")
    xray = svc.add_task(win, WorkType.FLAW_DETECTION, "C3",
                        tower_id="T1", device_id="D1")
    insulator = svc.add_task(win, WorkType.INSULATOR, "C2",
                             tower_id="T1", device_id="D1",
                             depends_on=[xray.id])
    fault_dev = svc.add_task(win, WorkType.FAULT_DEVICE, "C2",
                             tower_id="T2", device_id="D3")
    manual = svc.add_task(win, WorkType.MANUAL_INSPECTION, "C3",
                          tower_id="T1", device_id="D1",
                          depends_on=[insulator.id])
    drone = svc.add_task(win, WorkType.DRONE_REVIEW, "C3",
                         tower_id="T1", device_id="D1",
                         depends_on=[insulator.id])
    return {
        "foundation": foundation.id,
        "xray": xray.id,
        "insulator": insulator.id,
        "fault_dev": fault_dev.id,
        "manual": manual.id,
        "drone": drone.id,
    }


def finish(svc: MaintenanceService, task_id: str, person: str,
           cmd: str, verdict: Verdict | None = Verdict.PASS,
           detail: str = "合格") -> None:
    svc.start_task(task_id, person, f"{cmd}-S")
    svc.complete_task(task_id, person, verdict, detail, f"{cmd}-C")


class FullFlowTest(unittest.TestCase):
    def test_happy_path_and_release_basis(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)

        # 依赖未完成不能抢先干绝缘子
        with self.assertRaisesRegex(MaintenanceError, "前置工序未完成"):
            svc.start_task(t["insulator"], "P2", "BAD-1")

        finish(svc, t["foundation"], "P1", "F1")
        finish(svc, t["xray"], "P3", "X1")
        finish(svc, t["insulator"], "P2", "I1")
        finish(svc, t["fault_dev"], "P2", "FD1")
        finish(svc, t["manual"], "P3", "M1")
        finish(svc, t["drone"], "P4", "D1")

        receipt = svc.restore_power(win, "P5", "CMD-9")
        self.assertTrue(receipt.accepted)
        window = svc.windows[win]
        self.assertIs(window.state, WindowState.RESTORED)
        basis = window.release_basis
        self.assertEqual(basis.restored_by, "P5")
        self.assertEqual(basis.approved_outage_by, "P5")
        self.assertEqual(basis.site_lock_by, "P1")
        self.assertEqual(set(basis.closed_task_ids), set(t.values()))

        # 已恢复窗口不允许再次送电
        with self.assertRaisesRegex(MaintenanceError, "禁止重复送电"):
            svc.restore_power(win, "P5", "CMD-10")

    def test_trace_from_line_to_tower_and_basis(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        for key, person, cmd in [
            ("foundation", "P1", "F1"), ("xray", "P3", "X1"),
            ("insulator", "P2", "I1"), ("fault_dev", "P2", "FD1"),
            ("manual", "P3", "M1"), ("drone", "P4", "D1"),
        ]:
            finish(svc, t[key], person, cmd)
        svc.restore_power(win, "P5", "CMD-9")

        overview = svc.line_overview("L1")
        section = overview["sections"][0]
        self.assertEqual(section["section"]["isolation_zone"], "Z1")
        tower1 = section["towers"][0]
        self.assertEqual(tower1["tower"]["id"], "T1")
        device1 = tower1["devices"][0]
        self.assertEqual(device1["device"]["id"], "D1")
        # 设备上能追到每项试验证据
        sources = {e["source"] for e in device1["evidence"]}
        self.assertEqual(sources, {"xray", "manual", "drone"})
        # 窗口上能追到最终放行依据
        basis = section["windows"][0]["release_basis"]
        self.assertIn(t["xray"], basis["closed_task_ids"])


class AuthorizationTest(unittest.TestCase):
    def test_only_dispatcher_approves(self):
        svc = build_world()
        svc.add_risk_ticket("R1", "S1", ALL_WORKS, "Z1", approved=True)
        win = svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")
        with self.assertRaisesRegex(MaintenanceError, "缺少资质 dispatch"):
            svc.approve_outage(win, "P1", "CMD-X")

    def test_crew_can_only_update_own_task(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        # 基建队的人不能更新检测中心的探伤作业
        with self.assertRaisesRegex(MaintenanceError, "只能更新本队的作业"):
            svc.start_task(t["xray"], "P1", "CMD-X")
        # 同队但无对应资质也不行
        with self.assertRaisesRegex(MaintenanceError, "缺少资质"):
            svc.start_task(t["foundation"], "P7", "CMD-Y")

    def test_cannot_start_before_lock(self):
        svc = build_world()
        svc.add_risk_ticket("R1", "S1", ALL_WORKS, "Z1", approved=True)
        win = svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")
        task = svc.add_task(win, WorkType.FOUNDATION, "C1", tower_id="T1")
        with self.assertRaisesRegex(MaintenanceError, "未停电锁定"):
            svc.start_task(task.id, "P1", "CMD-X")

    def test_risk_ticket_must_cover_work(self):
        svc = build_world()
        svc.add_risk_ticket("R1", "S1", {WorkType.FOUNDATION}, "Z1", approved=True)
        win = svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")
        svc.add_task(win, WorkType.FOUNDATION, "C1", tower_id="T1")
        svc.add_task(win, WorkType.FLAW_DETECTION, "C3",
                     tower_id="T1", device_id="D1")
        with self.assertRaisesRegex(MaintenanceError, "无风险票覆盖"):
            svc.approve_outage(win, "P5", "CMD-X")


class DefectFlowTest(unittest.TestCase):
    def test_defect_pauses_followups_but_xray_conclusion_remains(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)

        finish(svc, t["foundation"], "P1", "F1")
        # 探伤发现缺陷：结论落盘，同时登记缺陷
        finish(svc, t["xray"], "P3", "X1", Verdict.DEFECT, "焊缝裂纹")
        self.assertIs(svc.tasks[t["xray"]].state, TaskState.DONE)
        xray_evidence = [e for e in svc.evidence if e.source == "xray"]
        self.assertEqual(len(xray_evidence), 1)
        self.assertIs(xray_evidence[0].verdict, Verdict.DEFECT)

        defect_id = next(iter(svc.defects))
        # 后续绝缘子工序被暂停，无法开工
        self.assertIs(svc.tasks[t["insulator"]].state, TaskState.PAUSED)
        self.assertEqual(svc.tasks[t["insulator"]].block_reason, defect_id)
        with self.assertRaisesRegex(MaintenanceError, "已被缺陷暂停"):
            svc.start_task(t["insulator"], "P2", "CMD-X")

        # 其他塔位的作业不受影响，可以继续
        finish(svc, t["fault_dev"], "P2", "FD1")

        # 缺陷不闭环不能送电
        with self.assertRaisesRegex(MaintenanceError, "未闭环工序"):
            svc.restore_power(win, "P5", "CMD-X")

        # 整改责任人钱运维，验收必须由他人完成
        svc.rectify_defect(defect_id, "P2", "CMD-R")
        with self.assertRaisesRegex(MaintenanceError, "不得验收自己整改的缺陷"):
            svc.accept_defect(defect_id, "P2", True, "CMD-A0")
        svc.accept_defect(defect_id, "P6", True, "CMD-A1")
        self.assertIs(svc.defects[defect_id].state, DefectState.ACCEPTED)
        self.assertEqual(svc.defects[defect_id].rectified_by, "P2")
        self.assertEqual(svc.defects[defect_id].accepted_by, "P6")

        # 暂停解除，剩余工序继续
        self.assertIs(svc.tasks[t["insulator"]].state, TaskState.PENDING)
        finish(svc, t["insulator"], "P2", "I1")
        finish(svc, t["manual"], "P3", "M1")
        finish(svc, t["drone"], "P4", "D1")
        svc.restore_power(win, "P5", "CMD-9")

        # 探伤结论与缺陷整改记录都还在，可追溯
        overview = svc.line_overview("L1")
        device1 = overview["sections"][0]["towers"][0]["devices"][0]
        self.assertEqual(len([e for e in device1["evidence"] if e["source"] == "xray"]), 1)
        defect_view = device1["defects"][0]
        self.assertEqual(defect_view["rectified_by"], "P2")
        self.assertEqual(defect_view["accepted_by"], "P6")

    def test_rejected_rectification_goes_back(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        finish(svc, t["foundation"], "P1", "F1")
        finish(svc, t["xray"], "P3", "X1", Verdict.DEFECT, "裂纹")
        defect_id = next(iter(svc.defects))
        svc.rectify_defect(defect_id, "P2", "CMD-R")
        svc.accept_defect(defect_id, "P6", False, "CMD-A", reason="复拍仍可见")
        self.assertIs(svc.defects[defect_id].state, DefectState.REJECTED)
        # 退回后工序仍暂停
        self.assertIs(svc.tasks[t["insulator"]].state, TaskState.PAUSED)
        with self.assertRaisesRegex(MaintenanceError, "未闭环工序"):
            svc.restore_power(win, "P5", "CMD-X")


class ConflictReviewTest(unittest.TestCase):
    def test_manual_drone_conflict_keeps_both_evidence_and_escalates(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        finish(svc, t["foundation"], "P1", "F1")
        finish(svc, t["xray"], "P3", "X1")
        finish(svc, t["insulator"], "P2", "I1")
        finish(svc, t["fault_dev"], "P2", "FD1")
        # 无人机判合格，人工精检判缺陷 → 冲突
        finish(svc, t["drone"], "P4", "DR", Verdict.PASS, "外观正常")
        finish(svc, t["manual"], "P3", "MN", Verdict.DEFECT, "均压环松动")

        review = next(iter(svc.reviews.values()))
        self.assertIs(review.state, ReviewState.OPEN)
        self.assertEqual(len(review.evidence_ids), 2)
        # 两份证据同时保留，谁也不覆盖谁
        device_evidence = [e for e in svc.evidence
                           if e.device_id == "D1" and e.source in ("manual", "drone")]
        self.assertEqual(len(device_evidence), 2)
        self.assertEqual({e.verdict for e in device_evidence},
                         {Verdict.PASS, Verdict.DEFECT})

        # 复核未结案不能送电
        with self.assertRaisesRegex(MaintenanceError, "冲突复核"):
            svc.restore_power(win, "P5", "CMD-X")

        # 复核关联缺陷，必须先整改验收闭环才能结案
        defect_id = review.resulting_defect_id
        with self.assertRaisesRegex(MaintenanceError, "关联缺陷未验收闭环"):
            svc.resolve_review(review.id, "P3", "以人工复拍为准", "CMD-V0")
        svc.rectify_defect(defect_id, "P2", "CMD-R")
        svc.accept_defect(defect_id, "P6", True, "CMD-A")
        svc.resolve_review(review.id, "P3", "复拍确认缺陷，以人工结论为准", "CMD-V")
        self.assertIs(review.state, ReviewState.CLOSED)

        svc.restore_power(win, "P5", "CMD-9")
        basis = svc.windows[win].release_basis
        self.assertIn(review.id, basis.resolved_review_ids)
        self.assertIn(defect_id, basis.accepted_defect_ids)


class IdempotencyAndWindowTest(unittest.TestCase):
    def test_duplicate_command_returns_same_receipt_without_side_effect(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        svc.start_task(t["foundation"], "P1", "CMD-S")
        first = svc.complete_task(t["foundation"], "P1", None, "基础浇筑完成", "CMD-C")
        second = svc.complete_task(t["foundation"], "P1", None, "基础浇筑完成", "CMD-C")
        self.assertEqual(first, second)
        self.assertIs(svc.tasks[t["foundation"]].state, TaskState.DONE)

        # 已恢复后重放送电命令：返回原回执，不报错也不产生新动作
        finish(svc, t["xray"], "P3", "X1")
        finish(svc, t["insulator"], "P2", "I1")
        finish(svc, t["fault_dev"], "P2", "FD1")
        finish(svc, t["manual"], "P3", "M1")
        finish(svc, t["drone"], "P4", "D1")
        restore1 = svc.restore_power(win, "P5", "CMD-R")
        restore2 = svc.restore_power(win, "P5", "CMD-R")
        self.assertEqual(restore1, restore2)

        # 同一命令编号挪作他用必须拒绝
        with self.assertRaisesRegex(MaintenanceError, "已用于其他命令"):
            svc.approve_outage(win, "P5", "CMD-C")

    def test_window_adjustment_only_before_approval(self):
        svc = build_world()
        svc.add_risk_ticket("R1", "S1", ALL_WORKS, "Z1", approved=True)
        win = svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")
        svc.adjust_window(win, "2026-10-01T09:00", "2026-10-01T19:00", "CMD-J")
        self.assertEqual(svc.windows[win].planned_start, "2026-10-01T09:00")
        svc.approve_outage(win, "P5", "CMD-A")
        with self.assertRaisesRegex(MaintenanceError, "禁止调整时间"):
            svc.adjust_window(win, "2026-10-01T10:00", "2026-10-01T20:00", "CMD-J2")

    def test_midcourse_fault_blocks_restoration(self):
        svc = build_world()
        win = plan_window(svc)
        t = standard_tasks(svc, win)
        approve_and_lock(svc, win)
        finish(svc, t["foundation"], "P1", "F1")
        # 作业中途T2设备突发故障
        receipt = svc.report_fault("D3", "P2", "装置接线松动", "CMD-FAULT")
        fault_defect = receipt.ref_id
        self.assertIs(svc.tasks[t["fault_dev"]].state, TaskState.PAUSED)
        # 故障证据保留
        self.assertTrue(any(e.source == "fault_report" for e in svc.evidence))

        finish(svc, t["xray"], "P3", "X1")
        finish(svc, t["insulator"], "P2", "I1")
        finish(svc, t["manual"], "P3", "M1")
        finish(svc, t["drone"], "P4", "D1")
        with self.assertRaisesRegex(MaintenanceError, "缺陷"):
            svc.restore_power(win, "P5", "CMD-X")

        svc.rectify_defect(fault_defect, "P2", "CMD-R")
        svc.accept_defect(fault_defect, "P6", True, "CMD-A")
        finish(svc, t["fault_dev"], "P2", "FD1")
        svc.restore_power(win, "P5", "CMD-9")

    def test_no_parallel_outage_in_same_isolation_zone(self):
        svc = build_world()
        svc.register_section("L1", "S2", "2号检修区段", isolation_zone="Z1")
        svc.register_tower("S2", "T3", "N201塔")
        svc.add_risk_ticket("R1", "S1", ALL_WORKS, "Z1", approved=True)
        svc.add_risk_ticket("R2", "S2", ALL_WORKS, "Z1", approved=True)
        w1 = svc.allocate_window("S1", "2026-10-01T08:00", "2026-10-01T18:00")
        w2 = svc.allocate_window("S2", "2026-10-01T08:00", "2026-10-01T18:00")
        svc.approve_outage(w1, "P5", "CMD-A1")
        with self.assertRaisesRegex(MaintenanceError, "禁止重复停电"):
            svc.approve_outage(w2, "P5", "CMD-A2")


if __name__ == "__main__":
    unittest.main()
