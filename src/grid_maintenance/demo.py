"""集中检修期端到端演示：天林Ⅰ线一个停电窗口的完整管控过程。

运行：python3 -m src.grid_maintenance.demo
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import (
    Device,
    EvidenceKind,
    Line,
    Person,
    Qualification,
    RiskTicket,
    Role,
    Section,
    Tower,
    Verdict,
)
from .errors import ControlError
from .service import ControlService
from .tracing import build_overview, render_overview


def build() -> ControlService:
    t0 = datetime(2026, 10, 12, 8, 0)
    svc = ControlService(t0)

    # 人员：负责人、调度员、两队作业人员、无人机操作手、验收人、第三方复核人
    svc.register_person(Person("p-mgr", "陈负责", (Role.MANAGER,)))
    svc.register_person(Person("p-dispatch", "周调度", (Role.DISPATCHER,)))
    svc.register_person(
        Person(
            "p-build",
            "赵基建",
            (Role.WORKER,),
            (Qualification("塔基/安装", "塔基施工"), Qualification("塔基/安装", "装置安装")),
            team_id="team-A",
        )
    )
    svc.register_person(
        Person(
            "p-maint",
            "钱运维",
            (Role.WORKER,),
            (Qualification("绝缘子", "绝缘子更换"), Qualification("人工精检", "人工检查")),
            team_id="team-B",
        )
    )
    svc.register_person(
        Person(
            "p-ndt",
            "孙探伤",
            (Role.WORKER,),
            (Qualification("X光II级", "X光探伤"),),
            team_id="team-C",
        )
    )
    svc.register_person(
        Person(
            "p-drone",
            "李飞手",
            (Role.PILOT, Role.WORKER),
            (Qualification("无人机执照", "无人机复核"),),
            team_id="team-D",
        )
    )
    # 吴验收同时也是作业人员：规则保证她不能验收自己整改的缺陷
    svc.register_person(
        Person(
            "p-accept",
            "吴验收",
            (Role.ACCEPTOR, Role.WORKER),
            (Qualification("绝缘子", "绝缘子更换"),),
            team_id="team-B",
        )
    )
    svc.register_person(
        Person(
            "p-feng",
            "冯验收",
            (Role.ACCEPTOR,),
        )
    )
    svc.register_person(
        Person(
            "p-review",
            "郑复核",
            (Role.MANAGER,),
            (Qualification("高级复核", "升级复核"),),
        )
    )

    # 线路结构：天林Ⅰ线 → Ⅰ段 → N12/N13 塔 → 绝缘子、故障诊断装置
    line = Line("L-TL1", "天林Ⅰ线", 500)
    section = Section("S1", "L-TL1", "天林Ⅰ线-成都段")
    for tid, name in (("T-N12", "N12塔"), ("T-N13", "N13塔")):
        tower = Tower(tid, "S1", name)
        tower.devices[f"D-ins-{tid[-3:]}"] = Device(
            f"D-ins-{tid[-3:]}", tid, "复合绝缘子串", "绝缘子"
        )
        tower.devices[f"D-fd-{tid[-3:]}"] = Device(
            f"D-fd-{tid[-3:]}", tid, "故障诊断装置", "在线监测"
        )
        section.towers[tid] = tower
    line.sections["S1"] = section
    svc.register_line(line)

    svc.register_ticket(
        RiskTicket("TK-1", "S1", "N12-N13停电作业票", ("两侧接地刀闸合位", "验电接地", "围栏挂牌"))
    )
    return svc


def run(svc: ControlService) -> None:
    t = svc.now
    later = lambda h: t + timedelta(hours=h)

    print("== 1. 按安全隔离范围（区段）分配窗口 ==")
    r = svc.allocate_window(
        command_id="cmd-001",
        actor_id="p-mgr",
        section_id="S1",
        planned_start=later(0),
        planned_end=later(10),
        window_id="W-1",
    )
    print("  ", r.result)

    print("== 2. 窗口调整：停电前可改，改完重复回执不重复执行 ==")
    svc.now = later(1)
    svc.reschedule_window(
        command_id="cmd-002", actor_id="p-mgr", window_id="W-1",
        planned_start=later(2), planned_end=later(12),
    )
    replay = svc.reschedule_window(
        command_id="cmd-002-retry", actor_id="p-mgr", window_id="W-1",
        planned_start=later(2), planned_end=later(12),
    )
    print("   重试回执 replay =", replay.replay, "revision =", replay.result["revision"])

    print("== 3. 调度员批准停电，负责人现场确认隔离+风险票并锁定 ==")
    svc.now = later(2)
    svc.approve_deenergization(command_id="cmd-003", dispatcher_id="p-dispatch", window_id="W-1")
    svc.confirm_isolation(
        command_id="cmd-004", actor_id="p-mgr", window_id="W-1", ticket_ids=["TK-1"]
    )
    try:
        svc.reschedule_window(
            command_id="cmd-005", actor_id="p-mgr", window_id="W-1",
            planned_start=later(3), planned_end=later(13),
        )
    except ControlError as exc:
        print("   锁定后调整被拒：", exc)

    print("== 4. 派五道工序并声明依赖：塔基→探伤→绝缘子→装置→无人机复核 ==")
    svc.create_operation(command_id="cmd-010", actor_id="p-mgr", window_id="W-1",
                         title="N12塔基施工", work_type="塔基施工", team_id="team-A",
                         target_tower_id="T-N12", op_id="OP-base")
    svc.create_operation(command_id="cmd-011", actor_id="p-mgr", window_id="W-1",
                         title="N12塔材X光探伤", work_type="X光探伤", team_id="team-C",
                         target_tower_id="T-N12", op_id="OP-ndt", depends_on=("OP-base",))
    svc.create_operation(command_id="cmd-012", actor_id="p-mgr", window_id="W-1",
                         title="N12绝缘子更换", work_type="绝缘子更换", team_id="team-B",
                         target_tower_id="T-N12", target_device_id="D-ins-N12",
                         op_id="OP-ins", depends_on=("OP-ndt",))
    svc.create_operation(command_id="cmd-013", actor_id="p-mgr", window_id="W-1",
                         title="N12故障诊断装置安装", work_type="装置安装", team_id="team-A",
                         target_tower_id="T-N12", target_device_id="D-fd-N12",
                         op_id="OP-fd", depends_on=("OP-base",))
    svc.create_operation(command_id="cmd-014", actor_id="p-mgr", window_id="W-1",
                         title="N12绝缘子无人机复核", work_type="无人机复核", team_id="team-D",
                         target_tower_id="T-N12", target_device_id="D-ins-N12",
                         op_id="OP-drone", depends_on=("OP-ins",))

    print("== 5. 越权与资质校验：B队不能动A队作业；未闭环前序不能开工 ==")
    try:
        svc.start_operation(command_id="cmd-020", actor_id="p-maint", op_id="OP-base")
    except ControlError as exc:
        print("   越权被拒：", exc)
    try:
        svc.start_operation(command_id="cmd-021", actor_id="p-ndt", op_id="OP-ndt")
    except ControlError as exc:
        print("   依赖未闭环被拒：", exc)

    print("== 6. 塔基完工→探伤合格并闭环（该结论之后永不消失）==")
    svc.start_operation(command_id="cmd-022", actor_id="p-build", op_id="OP-base")
    svc.complete_operation(command_id="cmd-023", actor_id="p-build", op_id="OP-base")
    svc.now = later(3)
    svc.start_operation(command_id="cmd-024", actor_id="p-ndt", op_id="OP-ndt")
    svc.record_evidence(command_id="cmd-025", actor_id="p-ndt", op_id="OP-ndt",
                        kind=EvidenceKind.XRAY, verdict=Verdict.PASS, detail="塔材焊缝无裂纹")
    svc.complete_operation(command_id="cmd-026", actor_id="p-ndt", op_id="OP-ndt")

    print("== 7. 绝缘子更换：人工检查不合格立缺陷，后续无人机工序自动暂停 ==")
    svc.now = later(4)
    svc.start_operation(command_id="cmd-030", actor_id="p-maint", op_id="OP-ins")
    svc.complete_operation(command_id="cmd-031", actor_id="p-maint", op_id="OP-ins")
    svc.record_evidence(command_id="cmd-032", actor_id="p-maint", op_id="OP-ins",
                        kind=EvidenceKind.MANUAL, verdict=Verdict.FAIL, detail="销钉缺失")
    print("   无人机工序状态：", svc.operations["OP-drone"].state.value,
          "/", svc.operations["OP-drone"].pause_reason)
    print("   探伤证据仍在：", svc.evidences and any(
        e.kind == EvidenceKind.XRAY for e in svc.evidences.values()))

    print("== 8. 无人机对同一设备复核结论相反：两份证据都保留并升级 ==")
    svc.record_evidence(command_id="cmd-033", actor_id="p-drone", op_id="OP-ins",
                        kind=EvidenceKind.DRONE, verdict=Verdict.PASS, detail="图像未见异常")
    conflict_id = next(iter(svc.conflicts))
    print("   生成复核单：", conflict_id, "；人工/无人机证据数 =",
          sum(1 for e in svc.evidences.values() if e.device_id == "D-ins-N12"))
    try:
        svc.approve_restoration(command_id="cmd-034", dispatcher_id="p-dispatch", window_id="W-1")
    except ControlError as exc:
        print("   未闭环送电被拒：", exc)

    print("== 9. 第三方升级复核判定不合格 → 补登/维持缺陷，原证据不删 ==")
    svc.resolve_conflict(command_id="cmd-035", actor_id="p-review",
                         conflict_id=conflict_id, verdict=Verdict.FAIL,
                         resolution="复核确认销钉缺失，人工结论正确")

    print("== 10. 整改与利益回避验收：自己整改自己验收被拒，由另一名验收人通过 ==")
    defect_id = next(
        d.defect_id for d in svc.defects.values() if d.device_id == "D-ins-N12"
    )
    # 吴验收兼具 team-B 作业身份，由她整改
    svc.rectify_defect(command_id="cmd-040", actor_id="p-accept",
                       defect_id=defect_id, note="补装销钉并复紧")
    try:
        svc.accept_defect(command_id="cmd-041", acceptor_id="p-accept",
                          defect_id=defect_id, accepted=True)
    except ControlError as exc:
        print("   自整改自验收被拒：", exc)
    svc.accept_defect(command_id="cmd-042", acceptor_id="p-feng",
                      defect_id=defect_id, accepted=True, remark="复紧力矩合格")
    print("   绝缘子工序状态：", svc.operations["OP-ins"].state.value,
          "；无人机工序：", svc.operations["OP-drone"].state.value)

    print("== 11. 中途故障：暂停窗口作业，排除后恢复，结论不丢 ==")
    svc.now = later(5)
    svc.start_operation(command_id="cmd-050", actor_id="p-build", op_id="OP-fd")
    svc.raise_incident(command_id="cmd-051", actor_id="p-build", window_id="W-1",
                       description="装置电源回路异常告警")
    try:
        svc.complete_operation(command_id="cmd-052", actor_id="p-build", op_id="OP-fd")
    except ControlError as exc:
        print("   故障暂停期间完工被拒：", exc)
    svc.resolve_incident(command_id="cmd-053", actor_id="p-build",
                         incident_id=next(iter(svc.incidents)), resolution="更换电源模块")
    svc.complete_operation(command_id="cmd-054", actor_id="p-build", op_id="OP-fd")

    print("== 12. 无人机工序恢复并完成（复核合格证据保留）==")
    svc.start_operation(command_id="cmd-055", actor_id="p-drone", op_id="OP-drone")
    svc.complete_operation(command_id="cmd-056", actor_id="p-drone", op_id="OP-drone")

    print("== 13. 调度员送电；重复回执回放、重复送电与伪造重放都被挡住 ==")
    svc.now = later(9)
    svc.approve_restoration(command_id="cmd-060", dispatcher_id="p-dispatch", window_id="W-1")
    # 网络重发：同键同内容，回放原回执，不产生第二次送电
    replay_r = svc.approve_restoration(
        command_id="cmd-060-resend", dispatcher_id="p-dispatch", window_id="W-1"
    )
    print("   送电回执重发 replay =", replay_r.replay, "；窗口终态 =",
          svc.windows["W-1"].state.value, "；送电时间仍为",
          f"{svc.windows['W-1'].restored_at:%H:%M}")
    # 同键伪造不同内容（盗用已成功命令的幂等键、改掉参数）→ 拒绝
    try:
        svc.allocate_window(
            command_id="cmd-forge", actor_id="p-mgr", section_id="S1",
            planned_start=later(0), planned_end=later(11), window_id="W-1",
        )
    except ControlError as exc:
        print("   伪造重放被拒：", exc)
    # 送电后若又收到一条停电回执，与原停电命令同键同内容：只回放、不执行
    stale = svc.approve_deenergization(
        command_id="cmd-062", dispatcher_id="p-dispatch", window_id="W-1"
    )
    print("   迟到停电回执 replay =", stale.replay, "；窗口保持",
          svc.windows["W-1"].state.value)

    print()
    print(render_overview(build_overview(svc, "L-TL1")))


if __name__ == "__main__":
    service = build()
    run(service)
