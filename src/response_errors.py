"""地质灾害响应服务的领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ConflictError(DomainError):
    """同一标识被赋予了不同内容，或对象状态冲突。"""


class PermissionDeniedError(DomainError):
    """岗位无权执行该操作。"""


class SeparationOfDutiesError(DomainError):
    """发布指令的人试图独自确认任务完成。"""


class CapacityError(DomainError):
    """安置点容量不足。"""


class InsufficientStockError(DomainError):
    """物资批次可用量不足。"""


class DoubleAllocationError(DomainError):
    """力量或物资被重复占用。"""


class InvalidStateError(DomainError):
    """当前状态不允许该操作。"""
