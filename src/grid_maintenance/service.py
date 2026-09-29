"""输电检修作业管控服务。

在一个进程内对象上实现全部业务不变量：

* 窗口按安全隔离范围（区段/隔离区）分配，调度员才能批准停送电；
* 现场安全隔离确认后锁定，方可开工；
* 人员资质与承包队归属双重校验，承包队只能更新自己的作业；
* 工序依赖未满足不得开工，新增缺陷暂停后续工序，但已完成结论与证据不可变；
* 同一设备人工检查与无人机结论冲突时两份证据都保留并升级复核；
* 缺陷整改与验收分离，验收人不得验收自己整改的缺陷；
* 窗口调整、重复回执、中途故障都不会造成重复送电或遗漏风险；
* 恢复送电时冻结放行依据，支持从线路总览逐级追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .model import (
    ConflictReview,
    Crew,
    Defect,
    DefectState,
    Device,
    Evidence,
    Line,
    Person,
    Qualification,
    Receipt,
    ReleaseBasis,
    ReviewState,
    RiskTicket,
    Section,
    Task,
    TaskState,
    Tower,
    Verdict,
    WindowState,
    WorkWindow,
    WORK_QUALIFICATION,
    WorkType,
)

EVIDENCE_SOURCE = {
    WorkType.FLAW_DETECTION: "xray",
    WorkType.DRONE_REVIEW: "drone",
    WorkType.MANUAL_INSPECTION: "manual",
}

INSPECTION_SOURCES = {"manual", "drone"}


class MaintenanceError(ValueError):
    """业务规则被违反。"""


@dataclass
class _Counter:
    value: int = 0

    def next(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.value:04d}"


class MaintenanceService:
    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._seq = _Counter()

        self.lines: dict[str, Line] = {}
        self.sections: dict[str, Section] = {}
        self.towers: dict[str, Tower] = {}
        self.devices: dict[str, Device] = {}
        self.crews: dict[str, Crew] = {}
        self.people: dict[str, Person] = {}
        self.risk_tickets: dict[str, RiskTicket] = {}
        self.windows: dict[str, WorkWindow] = {}
        self.tasks: dict[str, Task] = {}
        self.evidence: list[Evidence] = []          # 只追加，永不删除/改写
        self.defects: dict[str, Defect] = {}
        self.reviews: dict[str, ConflictReview] = {}
        self._receipts: dict[str, Receipt] = {}

    # ------------------------------------------------------------------
    # 基础登记
    # ------------------------------------------------------------------

    def register_line(self, line_id: str, name: str) -> Line:
        if line_id in self.lines:
            raise MaintenanceError(f"线路已存在：{line_id}")
        line = Line(line_id, name)
        self.lines[line_id] = line
        return line

    def register_section(self, line_id: str, section_id: str, name: str,
                         isolation_zone: str) -> Section:
        self._require(self.lines, line_id, "线路")
        if section_id in self.sections:
            raise MaintenanceError(f"区段已存在：{section_id}")
        section = Section(section_id, line_id, name, isolation_zone=isolation_zone)
        self.sections[section_id] = section
        self.lines[line_id].section_ids.append(section_id)
        return section

    def register_tower(self, section_id: str, tower_id: str, name: str) -> Tower:
        self._require(self.sections, section_id, "区段")
        if tower_id in self.towers:
            raise MaintenanceError(f"铁塔已存在：{tower_id}")
        tower = Tower(tower_id, section_id, name)
        self.towers[tower_id] = tower
        self.sections[section_id].tower_ids.append(tower_id)
        return tower

    def register_device(self, tower_id: str, device_id: str, name: str) -> Device:
        self._require(self.towers, tower_id, "铁塔")
        if device_id in self.devices:
            raise MaintenanceError(f"设备已存在：{device_id}")
        device = Device(device_id, tower_id, name)
        self.devices[device_id] = device
        self.towers[tower_id].device_ids.append(device_id)
        return device

    def register_crew(self, crew_id: str, name: str,
                      qualifications: set[Qualification]) -> Crew:
        crew = Crew(crew_id, name, set(qualifications))
        self.crews[crew_id] = crew
        return crew

    def register_person(self, person_id: str, name: str, crew_id: str,
                        qualifications: set[Qualification]) -> Person:
        self._require(self.crews, crew_id, "承包队")
        person = Person(person_id, name, crew_id, set(qualifications))
        self.people[person_id] = person
        return person

    def add_risk_ticket(self, ticket_id: str, section_id: str,
                        work_types: set[WorkType], isolation_zone: str,
                        approved: bool = False) -> RiskTicket:
        section = self._require(self.sections, section_id, "区段")
        if section.isolation_zone != isolation_zone:
            raise MaintenanceError("风险票隔离区与区段安全隔离范围不一致")
        ticket = RiskTicket(ticket_id, section_id, set(work_types),
                            isolation_zone, approved)
        self.risk_tickets[ticket_id] = ticket
        return ticket

    def approve_risk_ticket(self, ticket_id: str) -> RiskTicket:
        ticket = self._require(self.risk_tickets, ticket_id, "风险票")
        ticket.approved = True
        return ticket

    # ------------------------------------------------------------------
    # 窗口与工序
    # ------------------------------------------------------------------

    def allocate_window(self, section_id: str, planned_start: str,
                        planned_end: str) -> str:
        """按安全隔离范围分配停电窗口，返回窗口编号。"""
        section = self._require(self.sections, section_id, "区段")
        if planned_end <= planned_start:
            raise MaintenanceError("窗口结束时间必须晚于开始时间")
        window = WorkWindow(
            id=self._seq.next("WIN"),
            section_id=section_id,
            isolation_zone=section.isolation_zone,
            planned_start=planned_start,
            planned_end=planned_end,
        )
        self.windows[window.id] = window
        return window.id

    def adjust_window(self, window_id: str, planned_start: str,
                      planned_end: str, command_no: str) -> Receipt:
        """窗口调整只允许在批准停电前进行，锁定后一律拒绝。"""
        return self._idempotent(command_no, "adjust_window", window_id, lambda: (
            self._do_adjust_window(window_id, planned_start, planned_end)))

    def _do_adjust_window(self, window_id: str, start: str, end: str) -> str:
        window = self._require(self.windows, window_id, "窗口")
        if window.state is not WindowState.PLANNED:
            raise MaintenanceError("窗口已批准停电，禁止调整时间")
        if end <= start:
            raise MaintenanceError("窗口结束时间必须晚于开始时间")
        window.planned_start, window.planned_end = start, end
        return window.id

    def add_task(self, window_id: str, work_type: WorkType, crew_id: str,
                 tower_id: Optional[str] = None,
                 device_id: Optional[str] = None,
                 depends_on: Optional[list[str]] = None) -> Task:
        window = self._require(self.windows, window_id, "窗口")
        if window.state is WindowState.RESTORED:
            raise MaintenanceError("窗口已恢复送电，不得新增作业")
        crew = self._require(self.crews, crew_id, "承包队")
        if WORK_QUALIFICATION[work_type] not in crew.qualifications:
            raise MaintenanceError(f"承包队无 {work_type.value} 作业资质")
        if device_id is not None:
            device = self._require(self.devices, device_id, "设备")
            if tower_id is None:
                tower_id = device.tower_id
            elif device.tower_id != tower_id:
                raise MaintenanceError("设备与铁塔不匹配")
        if tower_id is not None:
            tower = self._require(self.towers, tower_id, "铁塔")
            if tower.section_id != window.section_id:
                raise MaintenanceError("铁塔不在窗口区段内")
        deps = list(depends_on or [])
        for dep_id in deps:
            self._require(self.tasks, dep_id, "前置工序")
        task = Task(
            id=self._seq.next("TASK"),
            window_id=window_id,
            work_type=work_type,
            crew_id=crew_id,
            tower_id=tower_id,
            device_id=device_id,
            depends_on=deps,
        )
        self.tasks[task.id] = task
        window.crew_ids.add(crew_id)
        return task

    # ------------------------------------------------------------------
    # 停送电与现场锁定
    # ------------------------------------------------------------------

    def approve_outage(self, window_id: str, dispatcher_id: str,
                       command_no: str) -> Receipt:
        return self._idempotent(command_no, "approve_outage", window_id, lambda: (
            self._do_approve_outage(window_id, dispatcher_id)))

    def _do_approve_outage(self, window_id: str, dispatcher_id: str) -> str:
        window = self._require(self.windows, window_id, "窗口")
        dispatcher = self._require(self.people, dispatcher_id, "人员")
        self._require_qualification(dispatcher, Qualification.DISPATCH)
        if window.state is not WindowState.PLANNED:
            raise MaintenanceError("窗口不是待批准状态")
        for other in self.windows.values():
            if (other.isolation_zone == window.isolation_zone
                    and other.id != window.id
                    and other.state in (WindowState.APPROVED, WindowState.OUTAGE)):
                raise MaintenanceError("同一安全隔离区已有窗口处于停电状态，禁止重复停电")
        self._assert_risks_covered(window)
        window.state = WindowState.APPROVED
        window.outage_approved_by = dispatcher_id
        return window.id

    def confirm_site_lock(self, window_id: str, person_id: str,
                          command_no: str) -> Receipt:
        """现场确认安全隔离措施到位并锁定，锁定后窗口进入停电实施。"""
        return self._idempotent(command_no, "confirm_site_lock", window_id, lambda: (
            self._do_confirm_site_lock(window_id, person_id)))

    def _do_confirm_site_lock(self, window_id: str, person_id: str) -> str:
        window = self._require(self.windows, window_id, "窗口")
        person = self._require(self.people, person_id, "人员")
        if person.crew_id not in window.crew_ids:
            raise MaintenanceError("只有本窗口作业队伍可确认现场隔离")
        if not (person.qualifications & {
                Qualification.CONSTRUCTION,
                Qualification.MAINTENANCE,
                Qualification.INSPECTION}):
            raise MaintenanceError("确认人缺少现场作业资质")
        if window.state is not WindowState.APPROVED:
            raise MaintenanceError("窗口尚未批准停电，不能现场锁定")
        window.state = WindowState.OUTAGE
        window.locked = True
        window.locked_by = person_id
        window.locked_at = self._clock()
        return window.id

    def restore_power(self, window_id: str, dispatcher_id: str,
                      command_no: str) -> Receipt:
        """恢复送电：全部闭环后冻结放行依据。已恢复窗口拒绝再次送电。"""
        return self._idempotent(command_no, "restore_power", window_id, lambda: (
            self._do_restore_power(window_id, dispatcher_id)))

    def _do_restore_power(self, window_id: str, dispatcher_id: str) -> str:
        window = self._require(self.windows, window_id, "窗口")
        dispatcher = self._require(self.people, dispatcher_id, "人员")
        self._require_qualification(dispatcher, Qualification.DISPATCH)
        if window.state is WindowState.RESTORED:
            raise MaintenanceError("窗口已恢复送电，禁止重复送电")
        if window.state is not WindowState.OUTAGE or not window.locked:
            raise MaintenanceError("窗口未在停电锁定状态，不能恢复送电")
        self._assert_risks_covered(window)

        blockers: list[str] = []
        window_tasks = [t for t in self.tasks.values() if t.window_id == window.id]
        open_tasks = [t.id for t in window_tasks if t.state is not TaskState.DONE]
        if open_tasks:
            blockers.append("存在未闭环工序：" + ",".join(sorted(open_tasks)))

        defects = self._window_defects(window)
        pending = [d.id for d in defects
                   if d.state in (DefectState.OPEN, DefectState.RECTIFIED,
                                  DefectState.REJECTED)]
        if pending:
            blockers.append("存在未验收闭环的缺陷：" + ",".join(sorted(pending)))

        reviews = self._window_reviews(window)
        open_reviews = [r.id for r in reviews if r.state is ReviewState.OPEN]
        if open_reviews:
            blockers.append("存在未结案的冲突复核：" + ",".join(sorted(open_reviews)))

        if blockers:
            raise MaintenanceError("禁止送电；" + "；".join(blockers))

        window.release_basis = ReleaseBasis(
            window_id=window.id,
            section_id=window.section_id,
            approved_outage_by=self._outage_approver(window),
            site_lock_by=window.locked_by or "",
            closed_task_ids=tuple(sorted(t.id for t in window_tasks)),
            accepted_defect_ids=tuple(sorted(d.id for d in defects)),
            resolved_review_ids=tuple(sorted(r.id for r in reviews)),
            restored_by=dispatcher_id,
            restored_at=self._clock(),
        )
        window.state = WindowState.RESTORED
        return window.id

    # ------------------------------------------------------------------
    # 作业执行
    # ------------------------------------------------------------------

    def start_task(self, task_id: str, person_id: str, command_no: str) -> Receipt:
        return self._idempotent(command_no, "start_task", task_id, lambda: (
            self._do_start_task(task_id, person_id)))

    def _do_start_task(self, task_id: str, person_id: str) -> str:
        task = self._require(self.tasks, task_id, "工序")
        person = self._require(self.people, person_id, "人员")
        window = self._require(self.windows, task.window_id, "窗口")
        if window.state is not WindowState.OUTAGE or not window.locked:
            raise MaintenanceError("窗口未停电锁定，禁止开工")
        if person.crew_id != task.crew_id:
            raise MaintenanceError("承包队只能更新本队的作业")
        self._require_qualification(person, WORK_QUALIFICATION[task.work_type])
        if task.state is TaskState.PAUSED:
            raise MaintenanceError(f"工序已被缺陷暂停（{task.block_reason}），闭环后自动恢复")
        if task.state is TaskState.DONE:
            raise MaintenanceError("工序已完成，结论不可改写")
        unfinished = [d for d in task.depends_on
                      if self.tasks[d].state is not TaskState.DONE]
        if unfinished:
            task.state = TaskState.BLOCKED
            raise MaintenanceError("前置工序未完成：" + ",".join(unfinished))
        task.state = TaskState.RUNNING
        task.assignee_id = person_id
        return task.id

    def complete_task(self, task_id: str, person_id: str,
                      verdict: Optional[Verdict], detail: str,
                      command_no: str) -> Receipt:
        return self._idempotent(command_no, "complete_task", task_id, lambda: (
            self._do_complete_task(task_id, person_id, verdict, detail)))

    def _do_complete_task(self, task_id: str, person_id: str,
                          verdict: Optional[Verdict], detail: str) -> str:
        task = self._require(self.tasks, task_id, "工序")
        person = self._require(self.people, person_id, "人员")
        if person.crew_id != task.crew_id:
            raise MaintenanceError("承包队只能更新本队的作业")
        self._require_qualification(person, WORK_QUALIFICATION[task.work_type])
        if task.state is TaskState.DONE:
            raise MaintenanceError("工序已完成，结论不可改写")
        if task.state is not TaskState.RUNNING:
            raise MaintenanceError("工序未在执行中，不能回执完工")

        source = EVIDENCE_SOURCE.get(task.work_type)
        new_evidence: Optional[Evidence] = None
        if source is not None:
            if task.device_id is None:
                raise MaintenanceError("检测类工序必须绑定设备")
            if verdict not in (Verdict.PASS, Verdict.DEFECT):
                raise MaintenanceError("检测工序必须给出合格/缺陷结论")
            new_evidence = Evidence(
                id=self._seq.next("EV"),
                task_id=task.id,
                device_id=task.device_id,
                source=source,
                verdict=verdict,
                recorded_by=person_id,
                recorded_at=self._clock(),
                detail=detail,
            )

        # 先落结论：工序完成、证据追加，之后任何缺陷处理都不回滚
        task.state = TaskState.DONE
        task.completed_by = person_id
        task.completed_at = self._clock()
        if new_evidence is not None:
            self.evidence.append(new_evidence)

        resulting_defect_id: Optional[str] = None
        if new_evidence is not None and new_evidence.verdict is Verdict.DEFECT:
            resulting_defect_id = self._open_defect(new_evidence, detail).id

        if new_evidence is not None:
            self._detect_conflict(new_evidence, resulting_defect_id)

        return task.id

    def report_fault(self, device_id: str, person_id: str, description: str,
                     command_no: str) -> Receipt:
        """中途故障：按缺陷登记并暂停后续工序，防止恢复送电时遗漏风险。"""
        return self._idempotent(command_no, "report_fault", device_id, lambda: (
            self._do_report_fault(device_id, person_id, description)))

    def _do_report_fault(self, device_id: str, person_id: str,
                         description: str) -> str:
        device = self._require(self.devices, device_id, "设备")
        person = self._require(self.people, person_id, "人员")
        record = Evidence(
            id=self._seq.next("EV"),
            task_id="",
            device_id=device_id,
            source="fault_report",
            verdict=Verdict.DEFECT,
            recorded_by=person_id,
            recorded_at=self._clock(),
            detail=description,
        )
        self.evidence.append(record)
        defect = self._open_defect(record, description)
        return defect.id

    # ------------------------------------------------------------------
    # 缺陷整改与验收（职责分离）
    # ------------------------------------------------------------------

    def rectify_defect(self, defect_id: str, person_id: str,
                       command_no: str) -> Receipt:
        return self._idempotent(command_no, "rectify_defect", defect_id, lambda: (
            self._do_rectify_defect(defect_id, person_id)))

    def _do_rectify_defect(self, defect_id: str, person_id: str) -> str:
        defect = self._require(self.defects, defect_id, "缺陷")
        person = self._require(self.people, person_id, "人员")
        if defect.state not in (DefectState.OPEN, DefectState.REJECTED):
            raise MaintenanceError("缺陷当前状态不允许整改")
        defect.state = DefectState.RECTIFIED
        defect.rectified_by = person_id
        defect.rectified_crew_id = person.crew_id
        defect.rectified_at = self._clock()
        defect.history.append(f"{self._clock()} {person_id} 完成整改")
        return defect.id

    def accept_defect(self, defect_id: str, person_id: str,
                      accepted: bool, command_no: str,
                      reason: str = "") -> Receipt:
        """验收闭环。验收人不得是该缺陷的整改责任人本人。"""
        return self._idempotent(command_no, "accept_defect", defect_id, lambda: (
            self._do_accept_defect(defect_id, person_id, accepted, reason)))

    def _do_accept_defect(self, defect_id: str, person_id: str,
                          accepted: bool, reason: str) -> str:
        defect = self._require(self.defects, defect_id, "缺陷")
        person = self._require(self.people, person_id, "人员")
        self._require_qualification(person, Qualification.ACCEPTANCE)
        if defect.state is not DefectState.RECTIFIED:
            raise MaintenanceError("缺陷尚未整改完成，不能验收")
        if defect.rectified_by == person_id:
            raise MaintenanceError("不得验收自己整改的缺陷")
        if accepted:
            defect.state = DefectState.ACCEPTED
            defect.accepted_by = person_id
            defect.accepted_at = self._clock()
            defect.history.append(f"{self._clock()} {person_id} 验收通过")
            self._resume_tasks(defect_id)
        else:
            defect.state = DefectState.REJECTED
            defect.reject_reason = reason
            defect.history.append(f"{self._clock()} {person_id} 验收退回：{reason}")
        return defect.id

    def resolve_review(self, review_id: str, person_id: str, conclusion: str,
                       command_no: str) -> Receipt:
        """冲突复核结案；若复核指向缺陷，缺陷未验收闭环不得结案。"""
        return self._idempotent(command_no, "resolve_review", review_id, lambda: (
            self._do_resolve_review(review_id, person_id, conclusion)))

    def _do_resolve_review(self, review_id: str, person_id: str,
                           conclusion: str) -> str:
        review = self._require(self.reviews, review_id, "复核单")
        person = self._require(self.people, person_id, "人员")
        self._require_qualification(person, Qualification.INSPECTION)
        if review.state is ReviewState.CLOSED:
            raise MaintenanceError("复核单已结案")
        if review.resulting_defect_id is not None:
            defect = self.defects[review.resulting_defect_id]
            if defect.state is not DefectState.ACCEPTED:
                raise MaintenanceError("复核关联缺陷未验收闭环，不能结案")
        review.state = ReviewState.CLOSED
        review.resolved_by = person_id
        review.resolved_at = self._clock()
        review.conclusion = conclusion
        return review.id

    # ------------------------------------------------------------------
    # 内部规则
    # ------------------------------------------------------------------

    def _open_defect(self, record: Evidence, description: str) -> Defect:
        device = self.devices[record.device_id]
        defect = Defect(
            id=self._seq.next("DEF"),
            device_id=record.device_id,
            tower_id=device.tower_id,
            evidence_id=record.id,
            description=description,
        )
        defect.history.append(f"{record.recorded_at} 依据证据 {record.id} 登记缺陷")
        self.defects[defect.id] = defect
        self._pause_follow_up_tasks(defect)
        return defect

    def _pause_follow_up_tasks(self, defect: Defect) -> None:
        """新增缺陷暂停后续工序；已完成工序（含探伤结论）保持不变。"""
        target_section = self._device_section(defect.device_id)
        for task in self.tasks.values():
            if task.state is TaskState.DONE or task.tower_id is None:
                continue
            window = self.windows[task.window_id]
            if window.state is WindowState.RESTORED:
                continue
            if self.towers[task.tower_id].section_id != target_section:
                continue
            same_target = (task.device_id == defect.device_id
                           or task.tower_id == defect.tower_id)
            if same_target and task.state is not TaskState.PAUSED:
                task.state = TaskState.PAUSED
                task.block_reason = defect.id

    def _resume_tasks(self, defect_id: str) -> None:
        for task in self.tasks.values():
            if task.state is TaskState.PAUSED and task.block_reason == defect_id:
                task.state = TaskState.PENDING
                task.block_reason = None

    def _detect_conflict(self, new_record: Evidence,
                         defect_id: Optional[str]) -> None:
        """同设备人工与无人机结论冲突：两份证据都保留，升级复核。"""
        if new_record.source not in INSPECTION_SOURCES:
            return
        for prior in self.evidence:
            if (prior.id == new_record.id
                    or prior.device_id != new_record.device_id
                    or prior.source not in INSPECTION_SOURCES
                    or prior.source == new_record.source):
                continue
            if prior.verdict is not new_record.verdict:
                review = ConflictReview(
                    id=self._seq.next("REV"),
                    device_id=new_record.device_id,
                    evidence_ids=[prior.id, new_record.id],
                    raised_at=self._clock(),
                    resulting_defect_id=defect_id,
                )
                self.reviews[review.id] = review
                return

    def _assert_risks_covered(self, window: WorkWindow) -> None:
        tickets = [t for t in self.risk_tickets.values()
                   if t.section_id == window.section_id
                   and t.isolation_zone == window.isolation_zone
                   and t.approved]
        if not tickets:
            raise MaintenanceError("窗口缺少已批准的风险票")
        uncovered = {
            t.work_type for t in self.tasks.values() if t.window_id == window.id
        }
        for ticket in tickets:
            uncovered -= ticket.work_types
        if uncovered:
            names = ",".join(sorted(w.value for w in uncovered))
            raise MaintenanceError(f"以下工序无风险票覆盖：{names}")

    def _window_defects(self, window: WorkWindow) -> list[Defect]:
        return [d for d in self.defects.values()
                if self.towers[d.tower_id].section_id == window.section_id]

    def _window_reviews(self, window: WorkWindow) -> list[ConflictReview]:
        device_ids = {
            dev_id
            for tower_id in self.sections[window.section_id].tower_ids
            for dev_id in self.towers[tower_id].device_ids
        }
        return [r for r in self.reviews.values() if r.device_id in device_ids]

    def _device_section(self, device_id: str) -> str:
        return self.towers[self.devices[device_id].tower_id].section_id

    def _outage_approver(self, window: WorkWindow) -> str:
        return window.outage_approved_by or ""

    @staticmethod
    def _require_qualification(person: Person, qualification: Qualification) -> None:
        if not person.has(qualification):
            raise MaintenanceError(
                f"{person.name} 缺少资质 {qualification.value}")

    @staticmethod
    def _require(store: dict, key: str, label: str):
        try:
            return store[key]
        except KeyError:
            raise MaintenanceError(f"{label}不存在：{key}") from None

    # ------------------------------------------------------------------
    # 幂等回执
    # ------------------------------------------------------------------

    def _idempotent(self, command_no: str, command: str, ref: str,
                    action: Callable[[], str]) -> Receipt:
        if not command_no:
            raise MaintenanceError("缺少命令编号")
        prior = self._receipts.get(command_no)
        if prior is not None:
            if prior.command != command:
                raise MaintenanceError(f"命令编号 {command_no} 已用于其他命令，拒绝重复回执")
            return prior  # 重复回执：原样返回，不再执行任何副作用
        try:
            result_ref = action()
        except MaintenanceError:
            raise
        receipt = Receipt(command_no, command, True, "已受理", result_ref or ref)
        self._receipts[command_no] = receipt
        return receipt

    # ------------------------------------------------------------------
    # 追溯
    # ------------------------------------------------------------------

    def line_overview(self, line_id: str) -> dict:
        """线路总览：可逐级下钻到铁塔、设备、试验、缺陷责任人与放行依据。"""
        line = self._require(self.lines, line_id, "线路")
        return {
            "line": {"id": line.id, "name": line.name},
            "sections": [self._section_view(sid) for sid in line.section_ids],
        }

    def _section_view(self, section_id: str) -> dict:
        section = self.sections[section_id]
        windows = [w for w in self.windows.values() if w.section_id == section_id]
        return {
            "section": {"id": section.id, "name": section.name,
                        "isolation_zone": section.isolation_zone},
            "towers": [self._tower_view(tid) for tid in section.tower_ids],
            "windows": [self._window_view(w.id) for w in windows],
        }

    def _tower_view(self, tower_id: str) -> dict:
        tower = self.towers[tower_id]
        return {
            "tower": {"id": tower.id, "name": tower.name},
            "devices": [self._device_view(did) for did in tower.device_ids],
            "tower_tasks": [self._task_view(t.id) for t in self.tasks.values()
                            if t.tower_id == tower.id and t.device_id is None],
        }

    def _device_view(self, device_id: str) -> dict:
        device = self.devices[device_id]
        return {
            "device": {"id": device.id, "name": device.name},
            "tasks": [self._task_view(t.id) for t in self.tasks.values()
                      if t.device_id == device_id],
            "evidence": [vars(e) for e in self.evidence
                         if e.device_id == device_id],
            "defects": [self._defect_view(d.id) for d in self.defects.values()
                        if d.device_id == device_id],
            "reviews": [vars(r) for r in self.reviews.values()
                        if r.device_id == device_id],
        }

    def _task_view(self, task_id: str) -> dict:
        task = self.tasks[task_id]
        return {
            "id": task.id,
            "work_type": task.work_type.value,
            "crew_id": task.crew_id,
            "state": task.state.value,
            "assignee_id": task.assignee_id,
            "completed_by": task.completed_by,
            "depends_on": task.depends_on,
            "block_reason": task.block_reason,
        }

    def _defect_view(self, defect_id: str) -> dict:
        defect = self.defects[defect_id]
        return {
            "id": defect.id,
            "state": defect.state.value,
            "evidence_id": defect.evidence_id,
            "rectified_by": defect.rectified_by,
            "rectified_crew_id": defect.rectified_crew_id,
            "accepted_by": defect.accepted_by,
            "history": list(defect.history),
        }

    def _window_view(self, window_id: str) -> dict:
        window = self.windows[window_id]
        basis = None
        if window.release_basis is not None:
            basis = vars(window.release_basis)
            basis["closed_task_ids"] = list(window.release_basis.closed_task_ids)
            basis["accepted_defect_ids"] = list(window.release_basis.accepted_defect_ids)
            basis["resolved_review_ids"] = list(window.release_basis.resolved_review_ids)
        return {
            "id": window.id,
            "state": window.state.value,
            "locked": window.locked,
            "locked_by": window.locked_by,
            "planned_start": window.planned_start,
            "planned_end": window.planned_end,
            "release_basis": basis,
        }
