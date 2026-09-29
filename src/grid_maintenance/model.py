"""作业管控领域模型：参与方、电网结构、窗口、工序、证据、缺陷与放行依据。

所有实体都是普通 dataclass，状态迁移由 ``service.ControlService`` 统一驱动，
模型本身只承载数据，不直接做规则判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------- 参与方


class Role(str, Enum):
    """平台角色。一个人可以身兼多职，但作业归属与职责互斥仍按规则校验。"""

    MANAGER = "检修负责人"          # 统筹：建窗口、派作业、确认现场
    DISPATCHER = "运维调度员"       # 唯一有权批准停、送电
    WORKER = "基建/运维/检测人员"   # 承包队成员，只能更新本队作业
    ACCEPTOR = "验收人员"           # 验收缺陷整改（不得验收本人整改项）
    PILOT = "无人机操作手"          # 提交无人机复核证据


@dataclass(frozen=True)
class Qualification:
    """一项资质及其适用作业类型、有效期。"""

    name: str
    work_type: str                 # 对应 Operation.work_type
    expires_at: Optional[datetime] = None

    def covers(self, work_type: str, now: datetime) -> bool:
        if self.work_type != work_type:
            return False
        return self.expires_at is None or self.expires_at >= now


@dataclass
class Person:
    person_id: str
    name: str
    roles: tuple[Role, ...]
    qualifications: tuple[Qualification, ...] = ()
    team_id: Optional[str] = None    # 所属承包队（作业归属校验用）

    def has_role(self, role: Role) -> bool:
        return role in self.roles

    def qualified_for(self, work_type: str, now: datetime) -> bool:
        return any(q.covers(work_type, now) for q in self.qualifications)


# ---------------------------------------------------------------- 电网结构


@dataclass
class Device:
    """设备：挂在某基铁塔上，如绝缘子串、避雷器、故障诊断装置。"""

    device_id: str
    tower_id: str
    name: str
    device_type: str


@dataclass
class Tower:
    """铁塔：塔基施工、探伤等以塔为粒度的作业挂在这里。"""

    tower_id: str
    section_id: str
    name: str
    devices: dict[str, Device] = field(default_factory=dict)


@dataclass
class Section:
    """线路区段：安全隔离与窗口分配的最小范围。"""

    section_id: str
    line_id: str
    name: str
    towers: dict[str, Tower] = field(default_factory=dict)


@dataclass
class Line:
    line_id: str
    name: str
    voltage_kv: int
    sections: dict[str, Section] = field(default_factory=dict)


# ---------------------------------------------------------------- 风险票


class RiskTicketStatus(str, Enum):
    ISSUED = "已出票"
    IN_FORCE = "生效中"      # 窗口停电隔离完成、现场确认后生效
    CLOSED = "已闭环"


@dataclass
class RiskTicket:
    ticket_id: str
    section_id: str
    title: str
    controls: tuple[str, ...]          # 必须落实的安全措施
    status: RiskTicketStatus = RiskTicketStatus.ISSUED
    closed_at: Optional[datetime] = None
    closed_by: Optional[str] = None


# ---------------------------------------------------------------- 窗口


class WindowState(str, Enum):
    PLANNED = "已排程"
    DEENERGIZED = "已停电"      # 调度员批准停电
    ISOLATED = "已隔离确认"     # 现场隔离+风险票确认，作业锁定在此状态下
    ACTIVE_HALT = "中途故障暂停"
    RESTORED = "已恢复送电"
    CANCELLED = "已取消"


@dataclass
class Window:
    """停电窗口：按区段（安全隔离范围）分配，时间可调但带电后锁定。"""

    window_id: str
    section_id: str
    planned_start: datetime
    planned_end: datetime
    state: WindowState = WindowState.PLANNED
    ticket_ids: list[str] = field(default_factory=list)
    # 隔离边界：停电后由现场确认锁定，防止窗口调整扩大/缩小范围
    isolated_towers: Optional[frozenset[str]] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    deenergized_by: Optional[str] = None
    deenergized_at: Optional[datetime] = None
    restored_by: Optional[str] = None
    restored_at: Optional[datetime] = None
    revision: int = 0


# ---------------------------------------------------------------- 工序/作业


class OperationState(str, Enum):
    PENDING = "待开工"
    IN_PROGRESS = "进行中"
    COMPLETED = "已完成"
    PAUSED = "暂停"          # 新增缺陷/中途故障触发，后续工序不得开工
    CLOSED = "已闭环"        # 完工且无未决缺陷，方可参与送电放行


@dataclass
class Operation:
    """承包队承担的一项作业，挂在铁塔或设备上。"""

    op_id: str
    window_id: str
    title: str
    work_type: str                     # 资质匹配键，如 塔基施工/绝缘子更换/X光探伤/装置安装/无人机复核
    team_id: str
    target_tower_id: str
    target_device_id: Optional[str] = None
    state: OperationState = OperationState.PENDING
    depends_on: tuple[str, ...] = ()   # 必须已闭环才能开工
    assignee: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    closed_at: Optional[datetime] = None
    pause_reason: Optional[str] = None
    # 作业过程中产生、且必须在闭环前消完的缺陷
    defect_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 检测证据


class EvidenceKind(str, Enum):
    MANUAL = "人工检查"
    XRAY = "X光探伤"
    DRONE = "无人机复核"
    REVIEW = "升级复核"
    OTHER = "其他检测"


class Verdict(str, Enum):
    PASS = "合格"
    FAIL = "不合格"


@dataclass(frozen=True)
class Evidence:
    """检测结果只追加、不可覆盖。

    后到的任何证据都不会删除先到的结论——即使新增缺陷暂停后续工序，
    已完成的探伤结论也原样保留。
    """

    evidence_id: str
    op_id: str
    device_id: Optional[str]
    tower_id: str
    kind: EvidenceKind
    verdict: Verdict
    recorded_by: str
    recorded_at: datetime
    detail: str = ""


class ConflictState(str, Enum):
    OPEN = "待升级复核"
    RESOLVED = "已升级判定"


@dataclass
class EvidenceConflict:
    """同一设备人工检查与无人机结论冲突：两份证据都保留，工单升级复核。"""

    conflict_id: str
    device_id: str
    tower_id: str
    manual_evidence_id: str
    drone_evidence_id: str
    state: ConflictState = ConflictState.OPEN
    raised_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None
    # 升级复核产生的新证据（第三份），不覆盖原两份
    review_evidence_id: Optional[str] = None


# ---------------------------------------------------------------- 缺陷


class DefectState(str, Enum):
    OPEN = "待整改"
    RECTIFIED = "已整改待验收"
    ACCEPTED = "验收通过已闭环"
    REJECTED = "验收驳回"          # 回到待整改，原整改记录保留
    DISMISSED = "复核改判销缺"      # 升级复核改判合格，缺陷关闭但证据保留

# 视为已闭环的缺陷状态
CLOSED_DEFECT_STATES = frozenset({DefectState.ACCEPTED, DefectState.DISMISSED})


@dataclass
class RectificationRecord:
    rectified_by: str
    rectified_at: datetime
    note: str


@dataclass
class Defect:
    defect_id: str
    device_id: Optional[str]
    tower_id: str
    section_id: str
    source_evidence_id: Optional[str]     # 由哪份检测发现（可空，巡视发现）
    raised_by: str
    raised_at: datetime
    description: str
    state: DefectState = DefectState.OPEN
    history: list[RectificationRecord] = field(default_factory=list)
    accepted_by: Optional[str] = None
    accepted_at: Optional[datetime] = None
    dismissed_by: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    related_op_id: Optional[str] = None   # 归属作业：该作业闭环前必须消完


# ---------------------------------------------------------------- 中途故障


class IncidentState(str, Enum):
    OPEN = "处置中"
    RESOLVED = "已排除"


@dataclass
class Incident:
    """作业中途故障/异常：暂停窗口内作业，排除后方可恢复，不撤销任何结论。"""

    incident_id: str
    window_id: str
    raised_by: str
    raised_at: datetime
    description: str
    state: IncidentState = IncidentState.OPEN
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None


# ---------------------------------------------------------------- 放行依据


@dataclass(frozen=True)
class ReleaseBasis:
    """送电放行的最终依据快照，随送电回执永久留存，供线路总览追溯。"""

    window_id: str
    released_by: str
    released_at: datetime
    operation_count: int
    defect_count: int
    evidence_count: int
    incident_count: int
    ticket_ids: tuple[str, ...]
    ticket_closed: bool
    last_command_id: str
    summary: str
