"""时间工具。

业务时间全部来自命令携带的逻辑时钟，服务不读取墙钟，保证迟到雨情、
断点重放和解除归档都能按当时的时间线复现。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8), "CST")


def now() -> str:
    """生成当前时间戳（仅用于种子数据与演示，命令路径不依赖它）。"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)


def format(value: datetime) -> str:
    return value.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def add_minutes(value: str, minutes: int) -> str:
    return format(parse(value) + timedelta(minutes=minutes))


def earlier(a: str, b: str) -> bool:
    return parse(a) < parse(b)
