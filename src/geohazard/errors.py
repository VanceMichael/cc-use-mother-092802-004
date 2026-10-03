"""领域异常：所有违反业务约束的操作都以 GeoHazardError 拒绝。"""

from __future__ import annotations


class GeoHazardError(Exception):
    """业务规则冲突。"""


class NotFoundError(GeoHazardError):
    """引用的实体不存在。"""


class PermissionDeniedError(GeoHazardError):
    """岗位无权执行该操作或查看该数据。"""


class IdempotentReplay(GeoHazardError):
    """重复回执：命令曾以同一幂等键成功处理，直接返回原结果，不产生新事件。"""

    def __init__(self, receipt: dict) -> None:
        super().__init__("重复回执，已按幂等处理")
        self.receipt = receipt
