"""岗位访问控制与敏感信息脱敏。

岗位：
- commander  应急指挥人员：全局态势、发布研判与指令、批准解除
- grid       网格员：仅本网格住户/游客、叫应与转移任务
- shelter    安置点岗位：仅本安置点容量与已到达人员的必要照护信息
- rescue     救援队伍：仅本队任务、路线与可领取物资
- logistics  物资保障岗位：批次入库、前置、领取与交接
- observer   观察员：脱敏汇总看板

敏感字段（身份号、联系电话、健康照护细节）只向该人员所属网格的网格员
和其实际到达安置点的安置岗位开放；其他岗位看到的是掩码。
"""

from __future__ import annotations

from typing import Any

from .errors import PermissionDeniedError

COMMANDER = "commander"
GRID = "grid"
SHELTER = "shelter"
RESCUE = "rescue"
LOGISTICS = "logistics"
OBSERVER = "observer"

ROLES = frozenset({COMMANDER, GRID, SHELTER, RESCUE, LOGISTICS, OBSERVER})

SENSITIVE_KEYS = ("id_number", "phone", "care_need")


def require_role(role: str, *allowed: str) -> None:
    if role not in ROLES:
        raise PermissionDeniedError(f"未知岗位: {role}")
    if role not in allowed:
        raise PermissionDeniedError(f"{role} 无权执行该操作")


def _mask(value: str) -> str:
    if not value:
        return value
    if len(value) <= 4:
        return "**"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def can_see_person(role: str, scope: str | None, person: dict[str, Any]) -> bool:
    """scope 为该岗位负责的网格或安置点标识。"""
    if role == COMMANDER:
        return True
    if role == GRID:
        return person.get("grid_id") == scope
    if role == SHELTER:
        return person.get("arrival_shelter") == scope
    return False


def view_person(role: str, scope: str | None, person: dict[str, Any]) -> dict[str, Any]:
    """按岗位投影人员信息：无权看敏感字段时返回掩码副本。"""
    view = dict(person)
    if can_see_person(role, scope, person):
        return view
    for key in SENSITIVE_KEYS:
        if view.get(key):
            view[key] = _mask(str(view[key]))
    return view


def view_persons(
    role: str, scope: str | None, persons: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [view_person(role, scope, person) for person in persons]
