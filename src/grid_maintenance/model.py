"""输电检修窗口管控的领域模型。

模型只描述状态与业务概念，所有状态变更由 ``service.MaintenanceService``
完成，保证约束集中在一处实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class WorkType(str, Enum):
    """检修工序类型。"""

    FOUNDATION = "foundation"          # 塔基施工
    INSULATOR = "insulator"            # 绝缘子更换
    FLAW_DETECTION = "flaw_detection"  # X光探伤
    FAULT_DEVICE = "fault_device"      # 故障诊断装置安装
    DRONE_REVIEW = "drone_review"      # 无人机复核
    MANUAL_INSPECTION = "manual_inspection"  # 人工精检


class Qualification(str, Enum):
    """人员资质。"""

    CONSTRUCTION = "construction"  # 基建施工
    MAINTENANCE = "maintenance"    # 运维检修
    INSPECTION = "inspection"      # 检测（探伤/巡检）
    DISPATCH = "dispatch"          # 调度（批准停送电）
    ACCEPTANCE = "acceptance"      # 验收


# 工序所需资质
WORK_QUALIFICATION: dict[WorkType, Qualification] = {
    WorkType.FOUNDATION: Qualification.CONSTRUCTION,
    WorkType.INSULATOR: Qualification.MAINTENANCE,
    WorkType.FLAW_DETECTION: Qualification.INSPECTION,
    WorkType.FAULT_DEVICE: Qualification.MAINTENANCE,
    WorkType.DRONE_REVIEW: Qualification.INSPECTION,
    WorkType.MANUAL_INSPECTION: Qualification.INSPECTION,
}


class WindowState(str, Enum):
    PLANNED = "planned"        # 已计划
    APPROVED = "approved"      # 调度批准停电，等待实施
    OUTAGE = "outage"          # 停电实施中（已隔离锁定）
    RESTORED = "restored"      # 已恢复送电


class TaskState(str, Enum):
    PENDING = "pending"      # 待开工
    RUNNING = "running"      # 作业中
    DONE = "done"            # 已完成
    PAUSED = "paused"        # 因新增缺陷暂停
    BLOCKED = "blocked"      # 前置未满足


class Verdict(str, Enum):
    PASS = "pass"        # 合格
    DEFECT = "defect"    # 发现缺陷
    CONFLICT = "conflict"  # 与既有证据冲突，已升级复核
    PENDING = "pending"  # 冲突升级后等待复核结论


class DefectState(str, Enum):
    OPEN = "open"            # 待整改
    RECTIFIED = "rectified"  # 已整改，待验收
    ACCEPTED = "accepted"    # 已验收闭环
    REJECTED = "rejected"    # 验收不通过，退回整改


class ReviewState(str, Enum):
    """同设备人工检查与无人机结果冲突后的升级复核。"""

    OPEN = "open"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# 组织与资源
# ---------------------------------------------------------------------------


@dataclass
class Crew:
    """承包队。"""

    id: str
    name: str
    qualifications: set[Qualification] = field(default_factory=set)


@dataclass
class Person:
    id: str
    name: str
    crew_id: str
    qualifications: set[Qualification] = field(default_factory=set)

    def has(self, qualification: Qualification) -> bool:
        return qualification in self.qualifications


# ---------------------------------------------------------------------------
# 线路结构
# ---------------------------------------------------------------------------


@dataclass
class Device:
    """受检/受修设备，归属某基铁塔。"""

    id: str
    tower_id: str
    name: str


@dataclass
class Tower:
    """铁塔（每基可单独追溯）。"""

    id: str
    section_id: str
    name: str
    device_ids: list[str] = field(default_factory=list)


@dataclass
class Section:
    """线路区段，安全隔离范围以区段为单位。"""

    id: str
    line_id: str
    name: str
    tower_ids: list[str] = field(default_factory=list)
    isolation_zone: str = ""


@dataclass
class Line:
    id: str
    name: str
    section_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 风险票、窗口、作业
# ---------------------------------------------------------------------------


@dataclass
class RiskTicket:
    """风险票：作业前必须存在且覆盖对应工序与隔离区。"""

    id: str
    section_id: str
    work_types: set[WorkType]
    isolation_zone: str
    approved: bool = False


@dataclass
class WorkWindow:
    """停电窗口：按安全隔离范围（区段）分配。"""

    id: str
    section_id: str
    isolation_zone: str
    planned_start: str
    planned_end: str
    risk_ticket_ids: list[str] = field(default_factory=list)
    state: WindowState = WindowState.PLANNED
    crew_ids: set[str] = field(default_factory=set)
    outage_approved_by: Optional[str] = None  # 批准停电的调度员
    locked: bool = False          # 现场安全隔离确认后锁定
    locked_by: Optional[str] = None
    locked_at: Optional[str] = None
    # 放行依据在恢复送电时冻结（快照），之后任何状态变化都不改写它
    release_basis: Optional["ReleaseBasis"] = None


@dataclass
class Task:
    """窗口内一项作业工序。"""

    id: str
    window_id: str
    work_type: WorkType
    crew_id: str
    tower_id: Optional[str] = None
    device_id: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    state: TaskState = TaskState.PENDING
    assignee_id: Optional[str] = None
    completed_by: Optional[str] = None
    completed_at: Optional[str] = None
    block_reason: Optional[str] = None  # 暂停原因（如关联缺陷号）

    @property
    def closed(self) -> bool:
        return self.state is TaskState.DONE


# ---------------------------------------------------------------------------
# 证据、缺陷、复核
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """检测/检查证据。

    证据一经记录即不可变（frozen），后续缺陷与窗口调整都不能删除或改写；
    冲突时新旧两份证据同时保留。
    """

    id: str
    task_id: str
    device_id: str
    source: str          # "manual" | "drone" | "xray" 等
    verdict: Verdict
    recorded_by: str
    recorded_at: str
    detail: str = ""


@dataclass
class Defect:
    id: str
    device_id: str
    tower_id: str
    evidence_id: str
    description: str
    state: DefectState = DefectState.OPEN
    rectified_by: Optional[str] = None   # 整改责任人
    rectified_crew_id: Optional[str] = None
    rectified_at: Optional[str] = None
    accepted_by: Optional[str] = None
    accepted_at: Optional[str] = None
    reject_reason: Optional[str] = None
    history: list[str] = field(default_factory=list)


@dataclass
class ConflictReview:
    """同设备人工检查与无人机结论冲突的升级复核单，两份证据都保留。"""

    id: str
    device_id: str
    evidence_ids: list[str]
    state: ReviewState = ReviewState.OPEN
    raised_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    conclusion: Optional[str] = None
    resulting_defect_id: Optional[str] = None


@dataclass(frozen=True)
class ReleaseBasis:
    """恢复送电的最终放行依据（快照）。"""

    window_id: str
    section_id: str
    approved_outage_by: str
    site_lock_by: str
    closed_task_ids: tuple[str, ...]
    accepted_defect_ids: tuple[str, ...]
    resolved_review_ids: tuple[str, ...]
    restored_by: str
    restored_at: str


@dataclass(frozen=True)
class Receipt:
    """命令回执。相同命令编号重复提交时返回同一份回执，不产生副作用。"""

    receipt_no: str
    command: str
    accepted: bool
    message: str
    ref_id: Optional[str] = None
