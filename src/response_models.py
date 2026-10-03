"""响应过程使用的状态对象、状态常量与脱敏工具。"""

from __future__ import annotations

from dataclasses import dataclass


class ZoneKind:
    """点区双控：已登记灾害点与各类风险区同等管理。"""

    HAZARD_POINT = "hazard_point"  # 已登记灾害点
    GULLY_MOUTH = "gully_mouth"  # 山区沟口
    CLIFF_SLOPE = "cliff_slope"  # 临崖陡坡
    CONSTRUCTION_SITE = "construction_site"  # 施工工地
    SCENIC_AREA = "scenic_area"  # 旅游景区


ZONE_KINDS = frozenset(
    {
        ZoneKind.HAZARD_POINT,
        ZoneKind.GULLY_MOUTH,
        ZoneKind.CLIFF_SLOPE,
        ZoneKind.CONSTRUCTION_SITE,
        ZoneKind.SCENIC_AREA,
    }
)

RISK_LEVEL_NAMES = {0: "未分级", 1: "蓝色", 2: "黄色", 3: "橙色", 4: "红色"}
MIN_RISK_LEVEL = 1
MAX_RISK_LEVEL = 4


class GroupKind:
    HOUSEHOLD = "household"  # 住户
    TOURIST = "tourist"  # 游客


GROUP_KINDS = frozenset({GroupKind.HOUSEHOLD, GroupKind.TOURIST})


class TaskKind:
    VERIFY = "verify"  # 核查
    TRANSFER = "transfer"  # 转移
    PERSUADE = "persuade"  # 劝离（拒绝转移后的替代方案）
    DOOR_CHECK = "door_check"  # 上门核查（通信中断后的替代方案）


class TaskStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRM = "awaiting_confirm"
    CONFIRMED = "confirmed"
    REFUSED = "refused"
    NEEDS_REVIEW = "needs_review"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.CONFIRMED, TaskStatus.REFUSED})
ACTIVE_TRANSFER_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.AWAITING_CONFIRM,
        TaskStatus.NEEDS_REVIEW,
    }
)


class RouteStatus:
    OPEN = "open"
    CLOSED = "closed"


class PersonStatus:
    AT_RISK = "at_risk"
    EN_ROUTE = "en_route"
    SAFE = "safe"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"


class HoldStatus:
    HELD = "held"  # 已预占
    IN_TRANSIT = "in_transit"  # 已领取出库、交接途中
    RECEIVED = "received"  # 交接完成
    RELEASED = "released"  # 预占解除


@dataclass
class Grid:
    grid_id: str
    name: str
    member_ids: list[str]


@dataclass
class Zone:
    zone_id: str
    kind: str
    name: str
    grid_id: str
    primary_route_id: str | None = None
    primary_shelter_id: str | None = None


@dataclass
class Route:
    route_id: str
    origin_zone_id: str
    shelter_id: str
    status: str = RouteStatus.OPEN
    closed_reason: str | None = None


@dataclass
class Shelter:
    shelter_id: str
    name: str
    capacity: int
    occupancy: int = 0

    @property
    def remaining(self) -> int:
        return self.capacity - self.occupancy


@dataclass
class Group:
    group_id: str
    kind: str
    grid_id: str
    zone_id: str


@dataclass
class Person:
    person_id: str
    group_id: str
    name: str
    phone: str
    vulnerable: bool = False
    status: str = PersonStatus.AT_RISK
    shelter_id: str | None = None


@dataclass
class Campaign:
    """一次预警叫应及其目标分组。"""

    campaign_id: str
    message: str
    deadline: str
    scope_zone_ids: list[str] | None
    target_group_ids: list[str]
    issued_by: str


@dataclass
class Receipt:
    receipt_id: str
    campaign_id: str
    group_id: str
    reported_by: str
    note: str | None


@dataclass
class Order:
    """转移指令：发布人留痕，完成需他人确认。"""

    order_id: str
    issuer: str
    basis_assessment_id: str | None
    generated: bool


@dataclass
class Task:
    task_id: str
    kind: str
    status: str
    grid_id: str
    created_by: str
    zone_id: str | None = None
    group_id: str | None = None
    route_id: str | None = None
    shelter_id: str | None = None
    order_id: str | None = None
    origin: str | None = None
    confirmed_by: str | None = None
    note: str | None = None


@dataclass
class Team:
    team_id: str
    name: str
    home_district: str
    size: int
    active_deployment_id: str | None = None


@dataclass
class Deployment:
    deployment_id: str
    team_id: str
    to_district: str
    mission: str
    issued_by: str
    cross_district: bool
    active: bool = True


@dataclass
class Batch:
    batch_id: str
    kind: str
    quantity: int
    location_district: str


@dataclass
class Hold:
    """物资预占：预占、领取、交接三段状态，防止双重占用。"""

    hold_id: str
    batch_id: str
    quantity: int
    purpose: str
    status: str = HoldStatus.HELD


def mask_name(name: str) -> str:
    """姓名脱敏：只保留姓氏。"""
    return (name[:1] + "*") if name else "*"


def mask_phone(phone: str) -> str:
    """电话脱敏：只保留前三位与后两位。"""
    digits = phone.strip()
    if len(digits) < 7:
        return "***"
    return digits[:3] + "****" + digits[-2:]
