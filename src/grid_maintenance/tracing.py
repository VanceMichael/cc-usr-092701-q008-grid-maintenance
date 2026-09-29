"""线路总览追溯：从线路一路追到每基铁塔、每项试验、缺陷责任与最终放行依据。

层级为 线路 → 区段（窗口、风险票、放行依据）→ 铁塔 → 作业/设备 →
检测证据（含人工/无人机/升级复核）、缺陷（整改责任人、验收人）、冲突。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import (
    CLOSED_DEFECT_STATES,
    ConflictState,
    Line,
    OperationState,
    ReleaseBasis,
    RiskTicketStatus,
    WindowState,
)


@dataclass
class DeviceTrace:
    device_id: str
    name: str
    evidences: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    defects: list[dict] = field(default_factory=list)


@dataclass
class TowerTrace:
    tower_id: str
    name: str
    operations: list[dict] = field(default_factory=list)
    devices: dict[str, DeviceTrace] = field(default_factory=dict)
    open_blockers: list[str] = field(default_factory=list)


@dataclass
class WindowTrace:
    window_id: str
    state: str
    planned_start: str
    planned_end: str
    confirmed_by: Optional[str]
    restored_by: Optional[str]
    release: Optional[ReleaseBasis] = None


@dataclass
class SectionTrace:
    section_id: str
    name: str
    windows: list[WindowTrace] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    towers: dict[str, TowerTrace] = field(default_factory=dict)


@dataclass
class LineOverview:
    line_id: str
    name: str
    voltage_kv: int
    ready_to_restore: bool
    blocker: Optional[str] = None
    sections: list[SectionTrace] = field(default_factory=list)


def build_overview(service, line_id: str) -> LineOverview:
    """依据服务当前状态构造线路总览。service 为 ControlService。"""
    line: Line = service.lines.get(line_id)
    if line is None:
        raise KeyError(f"线路不存在：{line_id}")

    sections_out: list[SectionTrace] = []
    blockers: list[str] = []

    for section in line.sections.values():
        st = SectionTrace(section_id=section.section_id, name=section.name)

        # 窗口（优先给出可放行性的具体阻塞原因）
        for window in service.windows.values():
            if window.section_id != section.section_id:
                continue
            release = service.releases.get(window.window_id)
            wt = WindowTrace(
                window_id=window.window_id,
                state=window.state.value,
                planned_start=window.planned_start.strftime("%Y-%m-%d %H:%M"),
                planned_end=window.planned_end.strftime("%Y-%m-%d %H:%M"),
                confirmed_by=window.confirmed_by,
                restored_by=window.restored_by,
                release=release,
            )
            st.windows.append(wt)
            if window.state != WindowState.RESTORED:
                try:
                    service._assert_all_closed(window)
                except Exception as exc:
                    blockers.append(f"窗口 {window.window_id}：{exc}")

        # 风险票
        for ticket in service.tickets.values():
            if ticket.section_id != section.section_id:
                continue
            item = {
                "ticket_id": ticket.ticket_id,
                "title": ticket.title,
                "status": ticket.status.value,
                "closed_by": ticket.closed_by,
            }
            st.tickets.append(item)
            if ticket.status != RiskTicketStatus.CLOSED:
                blockers.append(f"风险票 {ticket.ticket_id} 为{ticket.status.value}")

        for tower in section.towers.values():
            tt = TowerTrace(tower_id=tower.tower_id, name=tower.name)
            ops = [
                o for o in service.operations.values()
                if o.target_tower_id == tower.tower_id
            ]
            for op in ops:
                person_name = _person_name(service, op.assignee)
                tt.operations.append(
                    {
                        "op_id": op.op_id,
                        "title": op.title,
                        "work_type": op.work_type,
                        "team_id": op.team_id,
                        "state": op.state.value,
                        "assignee": person_name,
                        "depends_on": list(op.depends_on),
                        "pause_reason": op.pause_reason,
                    }
                )
                if op.state != OperationState.CLOSED:
                    tt.open_blockers.append(f"作业 {op.op_id} {op.state.value}")

            # 设备 → 证据/冲突/缺陷
            for device in tower.devices.values():
                dt = DeviceTrace(device_id=device.device_id, name=device.name)
                for ev in service.evidences.values():
                    if ev.device_id == device.device_id:
                        dt.evidences.append(
                            {
                                "evidence_id": ev.evidence_id,
                                "kind": ev.kind.value,
                                "verdict": ev.verdict.value,
                                "by": _person_name(service, ev.recorded_by),
                                "at": ev.recorded_at.strftime("%m-%d %H:%M"),
                                "detail": ev.detail,
                            }
                        )
                for cf in service.conflicts.values():
                    if cf.device_id == device.device_id:
                        dt.conflicts.append(
                            {
                                "conflict_id": cf.conflict_id,
                                "state": cf.state.value,
                                "manual": cf.manual_evidence_id,
                                "drone": cf.drone_evidence_id,
                                "review": cf.review_evidence_id,
                                "resolved_by": _person_name(service, cf.resolved_by),
                                "resolution": cf.resolution,
                            }
                        )
                        if cf.state == ConflictState.OPEN:
                            tt.open_blockers.append(f"冲突 {cf.conflict_id} 待升级复核")
                for df in service.defects.values():
                    if df.device_id == device.device_id or (
                        df.device_id is None and df.tower_id == tower.tower_id
                    ):
                        rectifiers = [
                            {
                                "by": _person_name(service, r.rectified_by),
                                "at": r.rectified_at.strftime("%m-%d %H:%M"),
                                "note": r.note,
                            }
                            for r in df.history
                        ]
                        dt.defects.append(
                            {
                                "defect_id": df.defect_id,
                                "state": df.state.value,
                                "description": df.description,
                                "raised_by": _person_name(service, df.raised_by),
                                "rectifications": rectifiers,
                                "accepted_by": _person_name(service, df.accepted_by),
                                "dismissed_by": _person_name(service, df.dismissed_by),
                            }
                        )
                        if df.state not in CLOSED_DEFECT_STATES:
                            tt.open_blockers.append(f"缺陷 {df.defect_id} {df.state.value}")
                tt.devices[device.device_id] = dt
            st.towers[tower.tower_id] = tt
        sections_out.append(st)

    return LineOverview(
        line_id=line.line_id,
        name=line.name,
        voltage_kv=line.voltage_kv,
        ready_to_restore=not blockers,
        blocker=blockers[0] if blockers else None,
        sections=sections_out,
    )


def _person_name(service, person_id: Optional[str]) -> Optional[str]:
    if person_id is None:
        return None
    person = service.people.get(person_id)
    return person.name if person else person_id


def _display_width(text: str) -> int:
    """估算终端显示宽度：中文/全角字符按 2 列计。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text)


def render_overview(ov: LineOverview) -> str:
    """生成可直接阅读的中文追溯文本。"""
    lines: list[str] = []
    head = f"线路 {ov.name}（{ov.voltage_kv}kV，{ov.line_id}）"
    lines.append(head)
    lines.append("─" * _display_width(head))
    status = "具备送电条件" if ov.ready_to_restore else f"暂不能送电：{ov.blocker}"
    lines.append(f"总览状态：{status}")
    for si, section in enumerate(ov.sections, 1):
        lines.append("")
        lines.append(f"[{si}] 区段 {section.name}（{section.section_id}）")
        for ticket in section.tickets:
            lines.append(
                f"  风险票 {ticket['ticket_id']} {ticket['title']} → {ticket['status']}"
                + (f"，闭环人 {ticket['closed_by']}" if ticket["closed_by"] else "")
            )
        for win in section.windows:
            lines.append(
                f"  窗口 {win.window_id} {win.planned_start}~{win.planned_end} "
                f"状态：{win.state}，现场确认：{win.confirmed_by or '—'}"
            )
            if win.release is not None:
                r = win.release
                lines.append(
                    f"    └ 放行依据：{r.summary}；证据{r.evidence_count}份、"
                    f"故障{r.incident_count}起；批准送电 {r.released_by} @ {r.released_at:%Y-%m-%d %H:%M}；"
                    f"命令 {r.last_command_id}"
                )
        for tower in section.towers.values():
            lines.append(f"  └ 铁塔 {tower.name}（{tower.tower_id}）")
            for op in tower.operations:
                dep = f"，依赖 {','.join(op['depends_on'])}" if op["depends_on"] else ""
                who = f"，承担人 {op['assignee']}" if op["assignee"] else ""
                pause = f"，暂停原因：{op['pause_reason']}" if op["pause_reason"] else ""
                lines.append(
                    f"      作业 {op['op_id']} {op['title']}[{op['work_type']}] "
                    f"队 {op['team_id']}{who} → {op['state']}{dep}{pause}"
                )
            for device in tower.devices.values():
                lines.append(f"      设备 {device.name}（{device.device_id}）")
                for ev in device.evidences:
                    lines.append(
                        f"        · {ev['kind']}：{ev['verdict']}（{ev['by']}，{ev['at']}）{ev['detail']}"
                    )
                for cf in device.conflicts:
                    lines.append(
                        f"        ⚠ 冲突 {cf['conflict_id']}：{cf['state']}"
                        f"（人工 {cf['manual']} / 无人机 {cf['drone']}）"
                    )
                    if cf["review"]:
                        lines.append(
                            f"          升级复核 {cf['review']} 判定人 {cf['resolved_by']}：{cf['resolution']}"
                        )
                for df in device.defects:
                    lines.append(
                        f"        ▢ 缺陷 {df['defect_id']}：{df['description']} → {df['state']}"
                        f"（发现人 {df['raised_by']}）"
                    )
                    for rec in df["rectifications"]:
                        lines.append(
                            f"          整改 {rec['by']} @ {rec['at']}：{rec['note']}"
                        )
                    if df["accepted_by"]:
                        lines.append(f"          验收人 {df['accepted_by']}（利益回避）")
                    if df["dismissed_by"]:
                        lines.append(f"          复核销缺人 {df['dismissed_by']}")
    return "\n".join(lines)
