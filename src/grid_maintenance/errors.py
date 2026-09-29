"""领域错误：所有违反作业管控规则的情况都以具体异常表达。"""

from __future__ import annotations


class ControlError(Exception):
    """作业管控规则冲突的基类。"""


class NotFoundError(ControlError):
    """引用的实体不存在。"""


class PermissionDeniedError(ControlError):
    """角色或承包归属不允许执行该操作。"""


class QualificationError(ControlError):
    """人员资质与作业要求不匹配，或资质已过期。"""


class WindowConflictError(ControlError):
    """窗口违反安全隔离范围，或时间区间非法。"""


class DependencyError(ControlError):
    """前序工序未闭环、存在暂停条件，不能开工或推进。"""


class EvidenceConflictError(ControlError):
    """同一设备的人工检查与无人机结果冲突（证据本身仍会保留）。"""


class ClosureError(ControlError):
    """缺陷或工序未闭环，不允许进入下一步。"""


class ReplayError(ControlError):
    """重复回执：同一幂等键的命令与首次内容不一致。"""


class StateError(ControlError):
    """实体当前状态不允许该操作（含重复送电）。"""
