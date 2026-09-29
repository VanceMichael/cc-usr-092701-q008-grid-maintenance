"""作业管控服务：集中检修期全部业务规则的唯一入口。

规则总览（与需求逐条对应）：

* 窗口按区段（安全隔离范围）分配；只有调度员能批准停、送电。
* 停电后现场确认锁定隔离边界与风险票，窗口时间此后不可再调整。
* 承包队只能更新本队作业；开工须资质有效、前序工序已闭环、无暂停条件。
* 检测结果只追加：新增缺陷可暂停后续工序，但已完成的探伤等结论永不消失。
* 同一设备人工检查与无人机结论冲突时，两份证据都保留并生成升级复核单。
* 缺陷整改后由非整改人验收（验收人员不得验收自己整改的缺陷）。
* 中途故障暂停窗口内全部作业，排除后恢复，过程中不撤销任何结论。
* 所有命令带幂等键：重复回执直接回放，内容不一致按重放拒绝；
  已恢复送电的窗口不能二次送电，未全闭环不能送电。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from itertools import count
from typing import Optional

from . import errors as err
from .ledger import Ledger, Receipt
from .model import (
    CLOSED_DEFECT_STATES,
    ConflictState,
    Defect,
    DefectState,
    Evidence,
    EvidenceConflict,
    EvidenceKind,
    Incident,
    IncidentState,
    Line,
    Operation,
    OperationState,
    Person,
    RectificationRecord,
    ReleaseBasis,
    RiskTicket,
    RiskTicketStatus,
    Role,
    Section,
    Tower,
    Verdict,
    Window,
    WindowState,
)


class ControlService:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.lines: dict[str, Line] = {}
        self.people: dict[str, Person] = {}
        self.tickets: dict[str, RiskTicket] = {}
        self.windows: dict[str, Window] = {}
        self.operations: dict[str, Operation] = {}
        self.evidences: dict[str, Evidence] = {}
        self.conflicts: dict[str, EvidenceConflict] = {}
        self.defects: dict[str, Defect] = {}
        self.incidents: dict[str, Incident] = {}
        self.releases: dict[str, ReleaseBasis] = {}
        self.ledger = Ledger()
        self._seq = count(1)

    # ------------------------------------------------------------ 基础资料

    def register_person(self, person: Person) -> None:
        self.people[person.person_id] = person

    def register_line(self, line: Line) -> None:
        self.lines[line.line_id] = line

    def register_ticket(self, ticket: RiskTicket) -> None:
        self._require_section(ticket.section_id)
        self.tickets[ticket.ticket_id] = ticket

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------ 查询辅助

    def _person(self, person_id: str) -> Person:
        person = self.people.get(person_id)
        if person is None:
            raise err.NotFoundError(f"人员不存在：{person_id}")
        return person

    def _require_role(self, person_id: str, role: Role) -> Person:
        person = self._person(person_id)
        if not person.has_role(role):
            raise err.PermissionDeniedError(f"{person.name} 不具有{role.value}角色")
        return person

    def _section(self, section_id: str) -> Section:
        for line in self.lines.values():
            section = line.sections.get(section_id)
            if section is not None:
                return section
        raise err.NotFoundError(f"区段不存在：{section_id}")

    def _require_section(self, section_id: str) -> Section:
        return self._section(section_id)

    def _tower(self, tower_id: str) -> Tower:
        for line in self.lines.values():
            for section in line.sections.values():
                tower = section.towers.get(tower_id)
                if tower is not None:
                    return tower
        raise err.NotFoundError(f"铁塔不存在：{tower_id}")

    def _window(self, window_id: str) -> Window:
        window = self.windows.get(window_id)
        if window is None:
            raise err.NotFoundError(f"窗口不存在：{window_id}")
        return window

    def _operation(self, op_id: str) -> Operation:
        op = self.operations.get(op_id)
        if op is None:
            raise err.NotFoundError(f"作业不存在：{op_id}")
        return op

    def _require_window_live(self, window_id: str) -> Window:
        """现场作业类操作只允许在已隔离确认的窗口进行。"""
        window = self._window(window_id)
        if window.state == WindowState.ACTIVE_HALT:
            raise err.DependencyError("窗口因中途故障暂停，暂停期间禁止现场作业")
        if window.state != WindowState.ISOLATED:
            raise err.StateError(f"窗口为{window.state.value}，现场作业条件不具备")
        return window

    # ------------------------------------------------------------ 幂等执行

    def _run(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        action: str,
        actor_id: str,
        payload: dict,
        fn,
    ) -> Receipt:
        """统一包裹：查重复回执 → 重放比对 → 执行并落账。"""
        prior = self.ledger.lookup(idempotency_key)
        fingerprint = self._fingerprint(action, payload)
        if prior is not None:
            if prior.fingerprint != fingerprint:
                receipt = self.ledger.record(
                    command_id=command_id,
                    idempotency_key=idempotency_key,
                    action=action,
                    actor_id=actor_id,
                    at=self.now,
                    ok=False,
                    result={},
                    fingerprint=fingerprint,
                    error="幂等键已被不同内容的命令使用，判定为重放攻击",
                )
                raise err.ReplayError(receipt.error)
            # 完全相同的重试：回放原回执，不重复执行任何副作用
            return self.ledger.record(
                command_id=command_id,
                idempotency_key=idempotency_key,
                action=action,
                actor_id=actor_id,
                at=self.now,
                ok=True,
                result=dict(prior.result),
                fingerprint=fingerprint,
                replay=True,
            )
        try:
            result = fn()
        except Exception as exc:  # 失败留痕但不占用幂等键
            self.ledger.record(
                command_id=command_id,
                idempotency_key=idempotency_key,
                action=action,
                actor_id=actor_id,
                at=self.now,
                ok=False,
                result={},
                fingerprint=fingerprint,
                error=str(exc),
            )
            raise
        return self.ledger.record(
            command_id=command_id,
            idempotency_key=idempotency_key,
            action=action,
            actor_id=actor_id,
            at=self.now,
            ok=True,
            result=result if isinstance(result, dict) else {"id": result},
            fingerprint=fingerprint,
        )

    @staticmethod
    def _fingerprint(action: str, payload: dict) -> str:
        return action + ":" + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------ 窗口分配

    def allocate_window(
        self,
        *,
        command_id: str,
        actor_id: str,
        section_id: str,
        planned_start: datetime,
        planned_end: datetime,
        window_id: Optional[str] = None,
    ) -> Receipt:
        """检修负责人按区段（安全隔离范围）分配停电窗口。"""
        payload = {
            "section_id": section_id,
            "start": planned_start,
            "end": planned_end,
            "window_id": window_id,
        }

        def do() -> dict:
            self._require_role(actor_id, Role.MANAGER)
            section = self._require_section(section_id)
            if planned_end <= planned_start:
                raise err.WindowConflictError("窗口结束时间必须晚于开始时间")
            # 同一隔离范围（区段）的窗口时间不得重叠，避免交叉作业
            for other in self.windows.values():
                if other.section_id != section_id or other.state == WindowState.CANCELLED:
                    continue
                if planned_start < other.planned_end and other.planned_start < planned_end:
                    raise err.WindowConflictError(
                        f"与同区段窗口 {other.window_id} 时间重叠，违反安全隔离范围"
                    )
            wid = window_id or self._new_id("WIN")
            if wid in self.windows:
                raise err.WindowConflictError(f"窗口编号已存在：{wid}")
            self.windows[wid] = Window(
                window_id=wid,
                section_id=section.section_id,
                planned_start=planned_start,
                planned_end=planned_end,
            )
            return {"window_id": wid, "section_id": section.section_id}

        return self._run(
            command_id=command_id,
            idempotency_key=f"allocate:{window_id or 'auto'}:{section_id}:{planned_start.isoformat()}",
            action="allocate_window",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def reschedule_window(
        self,
        *,
        command_id: str,
        actor_id: str,
        window_id: str,
        planned_start: datetime,
        planned_end: datetime,
    ) -> Receipt:
        """窗口调整：仅排程状态可调；现场确认锁定后任何调整一律拒绝。"""
        payload = {"window_id": window_id, "start": planned_start, "end": planned_end}

        def do() -> dict:
            self._require_role(actor_id, Role.MANAGER)
            window = self._window(window_id)
            if window.state in (WindowState.RESTORED, WindowState.CANCELLED):
                raise err.StateError(f"窗口{window.state.value}，不能调整")
            if window.state != WindowState.PLANNED:
                raise err.StateError(
                    f"窗口已{window.state.value}，隔离时序已启动，不能再调整时间"
                )
            if planned_end <= planned_start:
                raise err.WindowConflictError("窗口结束时间必须晚于开始时间")
            for other in self.windows.values():
                if other.window_id == window_id or other.state == WindowState.CANCELLED:
                    continue
                if other.section_id != window.section_id:
                    continue
                if planned_start < other.planned_end and other.planned_start < planned_end:
                    raise err.WindowConflictError(
                        f"调整后与同区段窗口 {other.window_id} 时间重叠"
                    )
            window.planned_start = planned_start
            window.planned_end = planned_end
            window.revision += 1
            return {"window_id": window_id, "revision": window.revision}

        return self._run(
            command_id=command_id,
            idempotency_key=f"reschedule:{window_id}:{planned_start.isoformat()}:{planned_end.isoformat()}",
            action="reschedule_window",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    # ------------------------------------------------------------ 停送电与现场确认

    def approve_deenergization(
        self, *, command_id: str, dispatcher_id: str, window_id: str
    ) -> Receipt:
        """调度员批准停电。"""
        payload = {"window_id": window_id}

        def do() -> dict:
            self._require_role(dispatcher_id, Role.DISPATCHER)
            window = self._window(window_id)
            if window.state == WindowState.DEENERGIZED:
                raise err.StateError("窗口已停电，请勿重复操作")
            if window.state != WindowState.PLANNED:
                raise err.StateError(f"窗口当前为{window.state.value}，不能批准停电")
            window.state = WindowState.DEENERGIZED
            window.deenergized_by = dispatcher_id
            window.deenergized_at = self.now
            return {"window_id": window_id, "state": window.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"deenergize:{window_id}",
            action="approve_deenergization",
            actor_id=dispatcher_id,
            payload=payload,
            fn=do,
        )

    def confirm_isolation(
        self,
        *,
        command_id: str,
        actor_id: str,
        window_id: str,
        ticket_ids: list[str],
    ) -> Receipt:
        """停电后现场确认：核对隔离范围与风险票，全部措施在票内并锁定边界。"""
        payload = {"window_id": window_id, "ticket_ids": sorted(ticket_ids)}

        def do() -> dict:
            self._require_role(actor_id, Role.MANAGER)
            window = self._window(window_id)
            if window.state != WindowState.DEENERGIZED:
                raise err.StateError("必须先由调度员批准停电，才能做现场隔离确认")
            if not ticket_ids:
                raise err.ClosureError("现场确认至少关联一张风险票")
            section = self._section(window.section_id)
            for tid in ticket_ids:
                ticket = self.tickets.get(tid)
                if ticket is None:
                    raise err.NotFoundError(f"风险票不存在：{tid}")
                if ticket.section_id != section.section_id:
                    raise err.WindowConflictError(
                        f"风险票 {tid} 不属于窗口隔离区段 {section.name}"
                    )
                if ticket.status != RiskTicketStatus.ISSUED:
                    raise err.StateError(f"风险票 {tid} 状态为{ticket.status.value}，不能用于确认")
                ticket.status = RiskTicketStatus.IN_FORCE
            window.state = WindowState.ISOLATED
            window.ticket_ids = list(ticket_ids)
            # 锁定隔离边界：当前区段内全部铁塔，之后窗口调整无法改变它
            window.isolated_towers = frozenset(section.towers)
            window.confirmed_by = actor_id
            window.confirmed_at = self.now
            return {
                "window_id": window_id,
                "state": window.state.value,
                "isolated_towers": sorted(window.isolated_towers),
            }

        return self._run(
            command_id=command_id,
            idempotency_key=f"isolate:{window_id}",
            action="confirm_isolation",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def approve_restoration(
        self, *, command_id: str, dispatcher_id: str, window_id: str
    ) -> Receipt:
        """调度员批准恢复送电——最终闸门：任何一项未闭环都不能送电。"""
        payload = {"window_id": window_id}

        def do() -> dict:
            self._require_role(dispatcher_id, Role.DISPATCHER)
            window = self._window(window_id)
            # 重复送电防护：RESTORED 是吸收态，任何重发/二次批准都直接拒绝
            if window.state == WindowState.RESTORED:
                raise err.StateError("窗口已恢复送电，禁止重复送电")
            if window.state not in (WindowState.ISOLATED,):
                raise err.StateError(f"窗口当前为{window.state.value}，不具备送电条件")
            self._assert_all_closed(window)
            # 关票
            for tid in window.ticket_ids:
                ticket = self.tickets[tid]
                ticket.status = RiskTicketStatus.CLOSED
                ticket.closed_at = self.now
                ticket.closed_by = dispatcher_id
            ops = [o for o in self.operations.values() if o.window_id == window_id]
            defects = [d for d in self._window_defects(window)]
            evidences = [
                e for e in self.evidences.values()
                if any(o.op_id == e.op_id for o in ops)
            ]
            incidents = [i for i in self.incidents.values() if i.window_id == window_id]
            basis = ReleaseBasis(
                window_id=window_id,
                released_by=dispatcher_id,
                released_at=self.now,
                operation_count=len(ops),
                defect_count=len(defects),
                evidence_count=len(evidences),
                incident_count=len(incidents),
                ticket_ids=tuple(window.ticket_ids),
                ticket_closed=True,
                last_command_id=command_id,
                summary=self._closure_summary(window),
            )
            self.releases[window_id] = basis
            window.state = WindowState.RESTORED
            window.restored_by = dispatcher_id
            window.restored_at = self.now
            return {
                "window_id": window_id,
                "state": window.state.value,
                "release_basis": {
                    "operations": basis.operation_count,
                    "defects": basis.defect_count,
                    "evidences": basis.evidence_count,
                    "incidents": basis.incident_count,
                    "tickets": list(basis.ticket_ids),
                },
            }

        return self._run(
            command_id=command_id,
            idempotency_key=f"restore:{window_id}",
            action="approve_restoration",
            actor_id=dispatcher_id,
            payload=payload,
            fn=do,
        )

    def _window_defects(self, window: Window) -> list[Defect]:
        """窗口范围内的全部缺陷（按窗口内作业的铁塔/设备归属）。"""
        ops = [o for o in self.operations.values() if o.window_id == window.window_id]
        tower_ids = {o.target_tower_id for o in ops}
        result = []
        seen: set[str] = set()
        for defect in self.defects.values():
            if defect.tower_id in tower_ids and defect.defect_id not in seen:
                result.append(defect)
                seen.add(defect.defect_id)
        return result

    def _assert_all_closed(self, window: Window) -> None:
        """逐项核对放行条件，给出第一条阻塞原因（任何一项未闭环都不放过）。"""
        if not window.ticket_ids:
            raise err.ClosureError("现场隔离与风险票尚未确认，不能送电")
        ops = [o for o in self.operations.values() if o.window_id == window.window_id]
        if not ops:
            raise err.ClosureError("窗口内没有任何作业记录，不能送电")
        for op in ops:
            if op.state != OperationState.CLOSED:
                raise err.ClosureError(f"作业 {op.title}（{op.op_id}）状态为{op.state.value}，未闭环")
        for defect in self._window_defects(window):
            if defect.state not in CLOSED_DEFECT_STATES:
                raise err.ClosureError(
                    f"缺陷 {defect.defect_id} 状态为{defect.state.value}，未闭环，不能送电"
                )
        for conflict in self.conflicts.values():
            if self._conflict_in_window(conflict, window) and conflict.state != ConflictState.RESOLVED:
                raise err.ClosureError(
                    f"设备 {conflict.device_id} 人工与无人机结论冲突尚未升级判定，不能送电"
                )
        for incident in self.incidents.values():
            if incident.window_id == window.window_id and incident.state != IncidentState.RESOLVED:
                raise err.ClosureError(f"中途故障 {incident.incident_id} 尚未排除，不能送电")

    def _closure_summary(self, window: Window) -> str:
        ops = [o for o in self.operations.values() if o.window_id == window.window_id]
        defects = self._window_defects(window)
        return "；".join(
            [
                f"作业{len(ops)}项全部闭环",
                f"缺陷{len(defects)}项全部验收通过",
                f"风险票{len(window.ticket_ids)}张已闭环",
            ]
        )

    def _conflict_in_window(self, conflict: EvidenceConflict, window: Window) -> bool:
        return any(
            o.window_id == window.window_id and o.target_tower_id == conflict.tower_id
            for o in self.operations.values()
        )

    # ------------------------------------------------------------ 作业派发与执行

    def create_operation(
        self,
        *,
        command_id: str,
        actor_id: str,
        window_id: str,
        title: str,
        work_type: str,
        team_id: str,
        target_tower_id: str,
        target_device_id: Optional[str] = None,
        depends_on: tuple[str, ...] = (),
        op_id: Optional[str] = None,
    ) -> Receipt:
        """检修负责人在窗口内派作业，并声明工序依赖。"""
        payload = {
            "window_id": window_id,
            "title": title,
            "work_type": work_type,
            "team_id": team_id,
            "tower": target_tower_id,
            "device": target_device_id,
            "depends": sorted(depends_on),
            "op_id": op_id,
        }

        def do() -> dict:
            self._require_role(actor_id, Role.MANAGER)
            window = self._window(window_id)
            if window.state in (WindowState.RESTORED, WindowState.CANCELLED):
                raise err.StateError(f"窗口{window.state.value}，不能再派发作业")
            tower = self._tower(target_tower_id)
            if target_device_id is not None and target_device_id not in tower.devices:
                raise err.NotFoundError(f"设备 {target_device_id} 不属于铁塔 {tower.name}")
            for dep in depends_on:
                if dep not in self.operations:
                    raise err.NotFoundError(f"依赖的前序作业不存在：{dep}")
            oid = op_id or self._new_id("OP")
            self.operations[oid] = Operation(
                op_id=oid,
                window_id=window.window_id,
                title=title,
                work_type=work_type,
                team_id=team_id,
                target_tower_id=tower.tower_id,
                target_device_id=target_device_id,
                depends_on=tuple(depends_on),
            )
            return {"op_id": oid, "window_id": window_id}

        return self._run(
            command_id=command_id,
            idempotency_key=f"create_op:{op_id or title}:{window_id}:{target_tower_id}:{target_device_id or ''}",
            action="create_operation",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def start_operation(
        self, *, command_id: str, actor_id: str, op_id: str
    ) -> Receipt:
        """承包队开工：归属队、资质、前序闭环、窗口状态全部满足才行。"""
        payload = {"op_id": op_id}

        def do() -> dict:
            person = self._person(actor_id)
            op = self._operation(op_id)
            if not person.has_role(Role.WORKER):
                raise err.PermissionDeniedError("只有承包队人员可以更新作业")
            if person.team_id != op.team_id:
                raise err.PermissionDeniedError(
                    f"{person.name} 不属于承包队 {op.team_id}，不能更新他人作业"
                )
            self._require_window_live(op.window_id)
            if op.state == OperationState.PAUSED:
                raise err.DependencyError(f"作业已暂停：{op.pause_reason}")
            if op.state != OperationState.PENDING:
                raise err.StateError(f"作业当前为{op.state.value}，不能开工")
            if not person.qualified_for(op.work_type, self.now):
                raise err.QualificationError(
                    f"{person.name} 缺少作业类型「{op.work_type}」的有效资质"
                )
            for dep_id in op.depends_on:
                dep = self.operations[dep_id]
                if dep.state != OperationState.CLOSED:
                    raise err.DependencyError(
                        f"前序作业 {dep.title}（{dep.state.value}）未闭环，不能开工"
                    )
            op.state = OperationState.IN_PROGRESS
            op.assignee = actor_id
            op.started_at = self.now
            return {"op_id": op_id, "state": op.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"start_op:{op_id}:{actor_id}",
            action="start_operation",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def complete_operation(
        self, *, command_id: str, actor_id: str, op_id: str
    ) -> Receipt:
        """承包队报完工。若产生过缺陷，作业停在已完成、待缺陷闭环后再关闭。"""
        payload = {"op_id": op_id}

        def do() -> dict:
            person = self._person(actor_id)
            op = self._operation(op_id)
            if not person.has_role(Role.WORKER) or person.team_id != op.team_id:
                raise err.PermissionDeniedError("只能由本承包队人员更新自己的作业")
            if op.state != OperationState.IN_PROGRESS:
                raise err.StateError(f"作业当前为{op.state.value}，不能报完工")
            self._require_window_live(op.window_id)
            op.state = OperationState.COMPLETED
            op.completed_at = self.now
            op.completed_by = actor_id
            self._try_close(op)
            return {"op_id": op_id, "state": op.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"complete_op:{op_id}",
            action="complete_operation",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def _try_close(self, op: Operation) -> None:
        """已完工且名下缺陷全部验收通过 → 作业闭环。"""
        if op.state != OperationState.COMPLETED:
            return
        own = [self.defects[d] for d in op.defect_ids if d in self.defects]
        if own and any(d.state not in CLOSED_DEFECT_STATES for d in own):
            return  # 保持已完成，等缺陷闭环
        if self._open_conflict_for_op(op) is not None:
            return  # 人工与无人机结论冲突待升级复核，作业不闭环
        op.state = OperationState.CLOSED
        op.closed_at = self.now

    def _open_conflict_for_op(self, op: Operation) -> Optional[EvidenceConflict]:
        evidence_ids = {e.evidence_id for e in self.evidences.values() if e.op_id == op.op_id}
        for conflict in self.conflicts.values():
            if conflict.state != ConflictState.OPEN:
                continue
            if conflict.manual_evidence_id in evidence_ids or conflict.drone_evidence_id in evidence_ids:
                return conflict
        return None

    # ------------------------------------------------------------ 检测证据

    def record_evidence(
        self,
        *,
        command_id: str,
        actor_id: str,
        op_id: str,
        kind: EvidenceKind,
        verdict: Verdict,
        detail: str = "",
        evidence_id: Optional[str] = None,
    ) -> Receipt:
        """登记检测结果（X光探伤、人工检查、无人机复核等）。

        证据只追加：任何新证据都不覆盖、不删除旧证据；
        人工检查与无人机对同一设备结论相反时，双证据留存并升级复核；
        结论为不合格时自动登记缺陷，暂停依赖该作业的后续工序。
        """
        payload = {
            "op_id": op_id,
            "kind": kind.value,
            "verdict": verdict.value,
            "detail": detail,
            "evidence_id": evidence_id,
        }

        def do() -> dict:
            person = self._person(actor_id)
            op = self._operation(op_id)
            if kind == EvidenceKind.DRONE:
                if not person.has_role(Role.PILOT) and not (
                    person.has_role(Role.WORKER) and person.team_id == op.team_id
                ):
                    raise err.PermissionDeniedError("无人机复核须由操作手或本队人员提交")
            else:
                if not person.has_role(Role.WORKER) or person.team_id != op.team_id:
                    raise err.PermissionDeniedError("只能由承担作业的承包队提交检测结果")
            if op.state not in (
                OperationState.IN_PROGRESS,
                OperationState.COMPLETED,
                OperationState.CLOSED,
            ):
                raise err.StateError(f"作业当前为{op.state.value}，不能登记检测结果")
            self._require_window_live(op.window_id)
            eid = evidence_id or self._new_id("EV")
            evidence = Evidence(
                evidence_id=eid,
                op_id=op_id,
                device_id=op.target_device_id,
                tower_id=op.target_tower_id,
                kind=kind,
                verdict=verdict,
                recorded_by=actor_id,
                recorded_at=self.now,
                detail=detail,
            )
            self.evidences[eid] = evidence  # 直接落库：append-only，永不删除
            defect_id: Optional[str] = None
            conflict_id: Optional[str] = None
            # 先判冲突：人工与无人机相反时以升级复核为准，暂不立案，避免错误暂停
            if op.target_device_id is not None:
                conflict_id = self._detect_conflict(evidence, actor_id)
            if verdict == Verdict.FAIL and conflict_id is None:
                section = self._section_of_tower(op.target_tower_id)
                defect_id = self._raise_defect(
                    op=op,
                    section_id=section.section_id,
                    source_evidence_id=eid,
                    raised_by=actor_id,
                    description=f"{kind.value}发现不合格：{detail or op.title}",
                )
            # 作业已闭环后又冒出冲突，退回已完成等待升级判定（证据不删）
            if conflict_id is not None and op.state == OperationState.CLOSED:
                op.state = OperationState.COMPLETED
                op.closed_at = None
            return {
                "evidence_id": eid,
                "defect_id": defect_id,
                "conflict_id": conflict_id,
            }

        return self._run(
            command_id=command_id,
            idempotency_key=(
                f"evidence:{evidence_id or 'auto'}:{op_id}:{kind.value}:{verdict.value}"
            ),
            action="record_evidence",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def _section_of_tower(self, tower_id: str) -> Section:
        for line in self.lines.values():
            for section in line.sections.values():
                if tower_id in section.towers:
                    return section
        raise err.NotFoundError(f"铁塔不存在：{tower_id}")

    def _raise_defect(
        self,
        *,
        op: Operation,
        section_id: str,
        source_evidence_id: Optional[str],
        raised_by: str,
        description: str,
    ) -> str:
        did = self._new_id("DEF")
        defect = Defect(
            defect_id=did,
            device_id=op.target_device_id,
            tower_id=op.target_tower_id,
            section_id=section_id,
            source_evidence_id=source_evidence_id,
            raised_by=raised_by,
            raised_at=self.now,
            description=description,
            related_op_id=op.op_id,
        )
        self.defects[did] = defect
        op.defect_ids.append(did)
        # 新增缺陷：暂停所有依赖本作业的后续工序（已完成的结论不受影响）
        for downstream in self.operations.values():
            if op.op_id in downstream.depends_on and downstream.state in (
                OperationState.PENDING,
                OperationState.IN_PROGRESS,
            ):
                downstream.state = OperationState.PAUSED
                downstream.pause_reason = f"前序作业 {op.title} 产生缺陷 {did}，待闭环"
        # 若本作业已完成，退回等待缺陷闭环（已登记的检测/完工事实不删）
        if op.state == OperationState.CLOSED:
            op.state = OperationState.COMPLETED
            op.closed_at = None
        return did

    def raise_defect(
        self,
        *,
        command_id: str,
        actor_id: str,
        op_id: str,
        description: str,
    ) -> Receipt:
        """巡视/现场人工新增缺陷（不依附某次检测），同样暂停后续工序。"""
        payload = {"op_id": op_id, "description": description}

        def do() -> dict:
            person = self._person(actor_id)
            op = self._operation(op_id)
            if not person.has_role(Role.WORKER) or person.team_id != op.team_id:
                raise err.PermissionDeniedError("只能对本队作业登记缺陷")
            self._require_window_live(op.window_id)
            section = self._section_of_tower(op.target_tower_id)
            did = self._raise_defect(
                op=op,
                section_id=section.section_id,
                source_evidence_id=None,
                raised_by=person.person_id,
                description=description,
            )
            return {"defect_id": did}

        return self._run(
            command_id=command_id,
            idempotency_key=f"defect:{op_id}:{self._digest(description)}",
            action="raise_defect",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def _detect_conflict(self, evidence: Evidence, actor_id: str) -> Optional[str]:
        """对照同设备上人工检查与无人机两类结论；相反则双证据留存、升级复核。"""
        if evidence.kind not in (EvidenceKind.MANUAL, EvidenceKind.DRONE):
            return None
        opposite_kind = (
            EvidenceKind.DRONE if evidence.kind == EvidenceKind.MANUAL else EvidenceKind.MANUAL
        )
        for prior in self.evidences.values():
            if prior.evidence_id == evidence.evidence_id:
                continue
            if prior.device_id != evidence.device_id or prior.device_id is None:
                continue
            if prior.kind != opposite_kind or prior.verdict == evidence.verdict:
                continue
            # 同一设备只保留一张在办复核单，避免重复证据重复升级
            if any(
                c.device_id == evidence.device_id and c.state == ConflictState.OPEN
                for c in self.conflicts.values()
            ):
                continue
            cid = self._new_id("CFL")
            manual = prior if evidence.kind == EvidenceKind.DRONE else evidence
            drone = evidence if evidence.kind == EvidenceKind.DRONE else prior
            conflict = EvidenceConflict(
                conflict_id=cid,
                device_id=evidence.device_id,
                tower_id=evidence.tower_id,
                manual_evidence_id=manual.evidence_id,
                drone_evidence_id=drone.evidence_id,
                raised_at=self.now,
            )
            self.conflicts[cid] = conflict
            return cid
        return None

    # ------------------------------------------------------------ 缺陷整改与验收

    def rectify_defect(
        self,
        *,
        command_id: str,
        actor_id: str,
        defect_id: str,
        note: str,
    ) -> Receipt:
        """承包队整改缺陷。只有先整改、再由他人验收，才能闭环。"""
        payload = {"defect_id": defect_id, "note": note}

        def do() -> dict:
            person = self._person(actor_id)
            defect = self.defects.get(defect_id)
            if defect is None:
                raise err.NotFoundError(f"缺陷不存在：{defect_id}")
            if not person.has_role(Role.WORKER):
                raise err.PermissionDeniedError("只有承包队人员可以整改缺陷")
            owner_team = (
                self.operations[defect.related_op_id].team_id
                if defect.related_op_id and defect.related_op_id in self.operations
                else None
            )
            if owner_team is not None and person.team_id != owner_team:
                raise err.PermissionDeniedError(
                    f"缺陷归属承包队 {owner_team}，不能由其他队伍整改"
                )
            if defect.related_op_id and defect.related_op_id in self.operations:
                self._require_window_live(self.operations[defect.related_op_id].window_id)
            if defect.state not in (DefectState.OPEN, DefectState.REJECTED):
                raise err.StateError(f"缺陷当前为{defect.state.value}，不能重复整改")
            defect.history.append(
                RectificationRecord(
                    rectified_by=actor_id, rectified_at=self.now, note=note
                )
            )
            defect.state = DefectState.RECTIFIED
            return {"defect_id": defect_id, "state": defect.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"rectify:{defect_id}:{len(self.defects[defect_id].history) if defect_id in self.defects else 0}",
            action="rectify_defect",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def accept_defect(
        self,
        *,
        command_id: str,
        acceptor_id: str,
        defect_id: str,
        accepted: bool,
        remark: str = "",
    ) -> Receipt:
        """验收人员验收整改：不得验收自己整改的缺陷；驳回可重新整改。"""
        payload = {"defect_id": defect_id, "accepted": accepted, "remark": remark}

        def do() -> dict:
            self._require_role(acceptor_id, Role.ACCEPTOR)
            defect = self.defects.get(defect_id)
            if defect is None:
                raise err.NotFoundError(f"缺陷不存在：{defect_id}")
            if defect.state != DefectState.RECTIFIED:
                raise err.StateError(f"缺陷当前为{defect.state.value}，尚无待验收整改")
            if defect.related_op_id and defect.related_op_id in self.operations:
                self._require_window_live(self.operations[defect.related_op_id].window_id)
            last_rectifier = defect.history[-1].rectified_by
            if last_rectifier == acceptor_id:
                raise err.PermissionDeniedError(
                    "验收人员不得验收自己整改的缺陷，必须由他人验收"
                )
            if accepted:
                defect.state = DefectState.ACCEPTED
                defect.accepted_by = acceptor_id
                defect.accepted_at = self.now
                # 缺陷闭环：恢复被暂停的后续工序，并尝试关闭归属作业
                if defect.related_op_id:
                    op = self.operations[defect.related_op_id]
                    self._try_close(op)
                    self._resume_downstream(op)
                result_state = defect.state.value
            else:
                defect.state = DefectState.REJECTED
                result_state = defect.state.value
            return {"defect_id": defect_id, "state": result_state, "remark": remark}

        return self._run(
            command_id=command_id,
            idempotency_key=f"accept:{defect_id}:{len(self.defects[defect_id].history) if defect_id in self.defects else 0}:{accepted}",
            action="accept_defect",
            actor_id=acceptor_id,
            payload=payload,
            fn=do,
        )

    def _resume_downstream(self, closed_op: Operation) -> None:
        """缺陷闭环后解除暂停：仅当所有前序作业都闭环时才恢复待开工。"""
        for downstream in self.operations.values():
            if closed_op.op_id not in downstream.depends_on:
                continue
            if downstream.state != OperationState.PAUSED:
                continue
            if all(
                self.operations[d].state == OperationState.CLOSED for d in downstream.depends_on
            ):
                downstream.state = OperationState.PENDING
                downstream.pause_reason = None

    def resolve_conflict(
        self,
        *,
        command_id: str,
        actor_id: str,
        conflict_id: str,
        verdict: Verdict,
        resolution: str,
    ) -> Receipt:
        """升级复核：由第三方（非原证据提交人）复检并给出判定，原两份证据保留。"""
        payload = {
            "conflict_id": conflict_id,
            "verdict": verdict.value,
            "resolution": resolution,
        }

        def do() -> dict:
            conflict = self.conflicts.get(conflict_id)
            if conflict is None:
                raise err.NotFoundError(f"复核单不存在：{conflict_id}")
            if conflict.state != ConflictState.OPEN:
                raise err.StateError("该冲突已升级判定，不得重复复核")
            person = self._person(actor_id)
            manual = self.evidences[conflict.manual_evidence_id]
            drone = self.evidences[conflict.drone_evidence_id]
            if actor_id in (manual.recorded_by, drone.recorded_by):
                raise err.PermissionDeniedError("升级复核须由原检查人之外的第三方执行")
            if not person.qualified_for("升级复核", self.now) and not person.has_role(Role.MANAGER):
                raise err.QualificationError("缺少「升级复核」资质，不能判定冲突")
            self._require_window_live(self.operations[manual.op_id].window_id)
            # 第三份证据独立留存，不覆盖前两份
            op = self.operations[manual.op_id]
            eid = self._new_id("EV")
            review = Evidence(
                evidence_id=eid,
                op_id=op.op_id,
                device_id=conflict.device_id,
                tower_id=conflict.tower_id,
                kind=EvidenceKind.REVIEW,
                verdict=verdict,
                recorded_by=actor_id,
                recorded_at=self.now,
                detail=f"升级复核判定：{resolution}",
            )
            self.evidences[eid] = review
            conflict.state = ConflictState.RESOLVED
            conflict.resolved_by = actor_id
            conflict.resolved_at = self.now
            conflict.resolution = resolution
            conflict.review_evidence_id = eid
            # 复核结论对设备上未闭环缺陷做处置（原两份冲突证据始终保留）
            open_defects = [
                d
                for d in self.defects.values()
                if d.device_id == conflict.device_id
                and d.state not in CLOSED_DEFECT_STATES
            ]
            if verdict == Verdict.FAIL:
                if not open_defects:
                    self._raise_defect(
                        op=op,
                        section_id=self._section_of_tower(conflict.tower_id).section_id,
                        source_evidence_id=eid,
                        raised_by=actor_id,
                        description=f"升级复核认定不合格：{resolution}",
                    )
            else:
                for defect in open_defects:
                    defect.state = DefectState.DISMISSED
                    defect.dismissed_by = actor_id
                    defect.dismissed_at = self.now
                if open_defects:
                    self._try_close(op)
                    self._resume_downstream(op)
            # 冲突解除后若无阻塞，尝试闭环
            self._try_close(op)
            return {"conflict_id": conflict_id, "review_evidence_id": eid}

        return self._run(
            command_id=command_id,
            idempotency_key=f"resolve_conflict:{conflict_id}",
            action="resolve_conflict",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    # ------------------------------------------------------------ 中途故障

    def raise_incident(
        self,
        *,
        command_id: str,
        actor_id: str,
        window_id: str,
        description: str,
    ) -> Receipt:
        """作业中途故障：窗口进入暂停，全部在制作业冻结（结论不撤销）。"""
        payload = {"window_id": window_id, "description": description}

        def do() -> dict:
            self._person(actor_id)
            window = self._window(window_id)
            if window.state != WindowState.ISOLATED:
                raise err.StateError(f"窗口为{window.state.value}，不能登记中途故障")
            iid = self._new_id("INC")
            self.incidents[iid] = Incident(
                incident_id=iid,
                window_id=window_id,
                raised_by=actor_id,
                raised_at=self.now,
                description=description,
            )
            window.state = WindowState.ACTIVE_HALT
            return {"incident_id": iid, "window_state": window.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"incident:{window_id}:{self._digest(description)}",
            action="raise_incident",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )

    def resolve_incident(
        self,
        *,
        command_id: str,
        actor_id: str,
        incident_id: str,
        resolution: str,
    ) -> Receipt:
        """排除故障后恢复窗口；作业按原状态继续，历史结论与证据原样有效。"""
        payload = {"incident_id": incident_id, "resolution": resolution}

        def do() -> dict:
            self._person(actor_id)
            incident = self.incidents.get(incident_id)
            if incident is None:
                raise err.NotFoundError(f"故障不存在：{incident_id}")
            if incident.state != IncidentState.OPEN:
                raise err.StateError("故障已排除，请勿重复回执")
            window = self._window(incident.window_id)
            incident.state = IncidentState.RESOLVED
            incident.resolved_by = actor_id
            incident.resolved_at = self.now
            incident.resolution = resolution
            window.state = WindowState.ISOLATED
            return {"incident_id": incident_id, "window_state": window.state.value}

        return self._run(
            command_id=command_id,
            idempotency_key=f"resolve_incident:{incident_id}",
            action="resolve_incident",
            actor_id=actor_id,
            payload=payload,
            fn=do,
        )
