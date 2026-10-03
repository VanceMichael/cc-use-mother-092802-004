"""岗位与角色：决定敏感人员信息的可见范围。

敏感人员信息只向相应网格和安置岗位开放，其余岗位（包括指挥
岗位）只能看到脱敏后的名册。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    COMMANDER = "commander"  # 应急指挥人员
    GRID_MEMBER = "grid_member"  # 区县和乡镇网格员
    SHELTER_STAFF = "shelter_staff"  # 安置点人员
    RESCUE_LEAD = "rescue_lead"  # 救援队伍
    LOGISTICS = "logistics"  # 物资保障
    AUDITOR = "auditor"  # 复盘审计


@dataclass(frozen=True)
class Principal:
    """一次操作的执行人及其岗位归属。"""

    user_id: str
    role: Role
    grid_id: str | None = None
    shelter_id: str | None = None
