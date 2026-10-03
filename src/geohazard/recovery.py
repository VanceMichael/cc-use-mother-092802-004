"""系统恢复：从账本文件重建服务并续办未竟事项。

恢复后自动列出：
- 逾期叫应：超过任务办理时限仍缺回执的人员，可继续补叫、登记失联；
- 待复核转移：已到达安置点、尚未复核入住的人员；
- 库存交接：已发起但未接收的交接，接收前点位保持冻结。
"""

from __future__ import annotations

from pathlib import Path

from .events import EventStore
from .service import ResponseService


def recover(path: str | Path) -> ResponseService:
    """从只追加账本重建内存状态，历史命令全部按原时间线重放。"""
    store = EventStore(Path(path))
    return ResponseService(store)


def resume_work(service: ResponseService, as_of: str) -> dict:
    """返回恢复时点的续办清单。"""
    return service.proj.resume(as_of)
