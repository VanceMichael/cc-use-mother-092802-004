"""地质灾害响应服务。

把雨情与风险研判、灾害点与风险区清单、责任网格、住户与游客分组、
叫应回执、转移路线、安置点容量、救援队伍和物资批次关联到同一条
只追加的事件链上：

- 事实先落盘后生效，系统恢复后按序回放，逾期叫应、待复核转移和
  库存交接可以继续办理；
- 迟到的雨情以新事件追加，可以改变后续措施，却不能改写当时已经
  发出的命令；
- 重复回执、重复到达、重复领取保持幂等，已到安全地点的人不会被
  重复计数；
- 发布指令的人不能独自确认任务完成；
- 敏感人员信息只向相应网格和安置岗位开放；
- 解除响应后可以从事件链还原当时的风险、人员、路线、力量和批准
  依据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from src.access import Principal, Role
from src.event_store import Event, EventStore
from src.response_errors import (
    CapacityError,
    ConflictError,
    DoubleAllocationError,
    InsufficientStockError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    SeparationOfDutiesError,
)
from src.response_models import (
    ACTIVE_TRANSFER_STATUSES,
    GROUP_KINDS,
    MAX_RISK_LEVEL,
    MIN_RISK_LEVEL,
    RISK_LEVEL_NAMES,
    TERMINAL_TASK_STATUSES,
    ZONE_KINDS,
    Batch,
    Campaign,
    Deployment,
    Grid,
    Group,
    Hold,
    HoldStatus,
    Order,
    Person,
    PersonStatus,
    Receipt,
    Route,
    RouteStatus,
    Shelter,
    Task,
    TaskKind,
    TaskStatus,
    Team,
    Zone,
    mask_name,
    mask_phone,
)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ResponseService:
    """地质灾害响应服务：命令验证规则，事件落盘后应用到状态。"""

    def __init__(self, store: EventStore, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._reset_state()
        for event in self.store.load():
            self._apply(event)

    @classmethod
    def open(cls, store_path: str | Path, clock: Callable[[], datetime] | None = None) -> "ResponseService":
        """打开（或创建）事件存储并回放恢复，系统重启后续办未了事项。"""
        return cls(EventStore(store_path), clock)

    @classmethod
    def _from_events(
        cls, events: Iterable[Event], clock: Callable[[], datetime] | None = None
    ) -> "ResponseService":
        """用事件子集重建某一时刻的状态，用于解除响应后的复盘还原。"""
        service = cls.__new__(cls)
        service.store = None
        service.clock = clock or (lambda: datetime.now(timezone.utc))
        service._reset_state()
        for event in events:
            service._apply(event)
        return service

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self.response: dict[str, Any] | None = None
        self.deactivated: dict[str, Any] | None = None
        self.risk_level = 0
        self.assessments: dict[str, dict[str, Any]] = {}
        self.last_assessment_recorded_at: str | None = None
        self.rainfall: dict[str, dict[str, Any]] = {}
        self.grids: dict[str, Grid] = {}
        self.zones: dict[str, Zone] = {}
        self.routes: dict[str, Route] = {}
        self.shelters: dict[str, Shelter] = {}
        self.groups: dict[str, Group] = {}
        self.persons: dict[str, Person] = {}
        self.campaigns: dict[str, Campaign] = {}
        self.receipts: dict[tuple[str, str], Receipt] = {}
        self.orders: dict[str, Order] = {}
        self.tasks: dict[str, Task] = {}
        self.teams: dict[str, Team] = {}
        self.deployments: dict[str, Deployment] = {}
        self.batches: dict[str, Batch] = {}
        self.holds: dict[str, Hold] = {}
        self._idem: dict[str, dict[str, Any]] = {}
        self._receipt_id_owners: dict[str, str] = {}

    def _now(self) -> datetime:
        return _aware(self.clock())

    def _now_iso(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _parse(moment: str) -> datetime:
        return _aware(datetime.fromisoformat(moment))

    # ------------------------------------------------------------------
    # 事件追加与回放
    # ------------------------------------------------------------------

    def _append(self, event_type: str, payload: dict[str, Any]) -> Event:
        assert self.store is not None, "复盘视图是只读的"
        event = self.store.append(event_type, payload, self._now_iso())
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise ValueError(f"未知事件类型 {event.type}")
        handler(event.payload, event)

    def _remember(self, key: str, payload: dict[str, Any], event: Event) -> None:
        self._idem[key] = {"event_seq": event.seq, "payload": payload}

    def _check_idem(self, key: str, payload: dict[str, Any], compare_keys: Iterable[str]) -> dict[str, Any] | None:
        """同一标识重复提交：内容一致则幂等返回，不一致则拒绝。"""
        existing = self._idem.get(key)
        if existing is None:
            return None
        old = existing["payload"]
        if any(old.get(field) != payload.get(field) for field in compare_keys):
            raise ConflictError(f"标识 {key} 已被不同内容使用")
        return {"duplicated": True, "event_seq": existing["event_seq"]}

    # ------------------------------------------------------------------
    # 守卫
    # ------------------------------------------------------------------

    def _require_active(self) -> None:
        if self.response is None:
            raise InvalidStateError("响应尚未启动")
        if self.deactivated is not None:
            raise InvalidStateError("响应已解除，只能复盘查询")

    def _require_mutable(self) -> None:
        if self.deactivated is not None:
            raise InvalidStateError("响应已解除，资料不得再修改")

    @staticmethod
    def _require_role(principal: Principal, *roles: Role) -> None:
        if principal.role not in roles:
            raise PermissionDeniedError(f"岗位 {principal.role.value} 无权执行该操作")

    def _grid(self, grid_id: str) -> Grid:
        grid = self.grids.get(grid_id)
        if grid is None:
            raise NotFoundError(f"网格不存在：{grid_id}")
        return grid

    def _zone(self, zone_id: str) -> Zone:
        zone = self.zones.get(zone_id)
        if zone is None:
            raise NotFoundError(f"灾害点或风险区不存在：{zone_id}")
        return zone

    def _route(self, route_id: str) -> Route:
        route = self.routes.get(route_id)
        if route is None:
            raise NotFoundError(f"转移路线不存在：{route_id}")
        return route

    def _shelter(self, shelter_id: str) -> Shelter:
        shelter = self.shelters.get(shelter_id)
        if shelter is None:
            raise NotFoundError(f"安置点不存在：{shelter_id}")
        return shelter

    def _group(self, group_id: str) -> Group:
        group = self.groups.get(group_id)
        if group is None:
            raise NotFoundError(f"分组不存在：{group_id}")
        return group

    def _task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return task

    def _team(self, team_id: str) -> Team:
        team = self.teams.get(team_id)
        if team is None:
            raise NotFoundError(f"救援队伍不存在：{team_id}")
        return team

    def _batch(self, batch_id: str) -> Batch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"物资批次不存在：{batch_id}")
        return batch

    def _hold(self, hold_id: str) -> Hold:
        hold = self.holds.get(hold_id)
        if hold is None:
            raise NotFoundError(f"物资预占不存在：{hold_id}")
        return hold

    def _group_members(self, group_id: str) -> list[Person]:
        return sorted(
            (person for person in self.persons.values() if person.group_id == group_id),
            key=lambda person: person.person_id,
        )

    def _unaccounted(self, group_id: str) -> list[Person]:
        """尚未确认安全的人员：交班时必须能逐一点出。"""
        return [person for person in self._group_members(group_id) if person.status != PersonStatus.SAFE]

    # ------------------------------------------------------------------
    # 响应生命周期
    # ------------------------------------------------------------------

    def activate_response(self, response_id: str, level: int, regions: list[str], by: Principal) -> dict[str, Any]:
        self._require_role(by, Role.COMMANDER)
        self._require_mutable()
        if self.response is not None:
            raise ConflictError("响应已经启动")
        if not response_id.strip() or not regions:
            raise InvalidStateError("响应标识与覆盖区域不能为空")
        payload = {
            "response_id": response_id,
            "level": level,
            "regions": list(regions),
            "by": by.user_id,
        }
        event = self._append("response_activated", payload)
        return {"duplicated": False, "event_seq": event.seq}

    def deactivate_response(self, deactivation_id: str, by: Principal, reason: str) -> dict[str, Any]:
        """解除响应：把当时未了事项一并留痕，供复盘还原。"""
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        payload = {
            "deactivation_id": deactivation_id,
            "reason": reason,
            "by": by.user_id,
            "open_items": {
                "overdue_alerts": self.overdue_alerts(),
                "unaccounted_person_ids": [person["person_id"] for person in self.unaccounted_persons()],
                "pending_review_task_ids": self.pending_review_transfers(),
                "pending_handover_hold_ids": self.pending_handover_holds(),
            },
        }
        duplicated = self._check_idem(f"deactivation:{deactivation_id}", payload, ("deactivation_id",))
        if duplicated is not None:
            return duplicated
        event = self._append("response_deactivated", payload)
        return {"duplicated": False, "event_seq": event.seq}

    def reconstruct_at(self, deactivation_id: str) -> dict[str, Any]:
        """从某次解除响应还原当时的风险、人员、路线、力量和批准依据。"""
        assert self.store is not None
        events = self.store.load()
        target = next(
            (
                event
                for event in events
                if event.type == "response_deactivated" and event.payload.get("deactivation_id") == deactivation_id
            ),
            None,
        )
        if target is None:
            raise NotFoundError(f"解除记录不存在：{deactivation_id}")
        past = self._from_events((event for event in events if event.seq <= target.seq), self.clock)
        by_status: dict[str, int] = {}
        for person in past.persons.values():
            by_status[person.status] = by_status.get(person.status, 0) + 1
        approvals = []
        for event in events:
            if event.seq > target.seq:
                break
            if event.type == "risk_assessed":
                approvals.append(
                    {
                        "seq": event.seq,
                        "kind": "risk_assessment",
                        "id": event.payload["assessment_id"],
                        "issued_by": event.payload["by"],
                        "basis": event.payload["basis"],
                    }
                )
            elif event.type == "transfer_ordered":
                approvals.append(
                    {
                        "seq": event.seq,
                        "kind": "transfer_order",
                        "id": event.payload["order_id"],
                        "issued_by": event.payload["issuer"],
                        "basis": {"assessment_id": event.payload.get("basis_assessment_id")},
                    }
                )
        return {
            "deactivation": target.payload,
            "risk": {
                "level": past.risk_level,
                "level_name": RISK_LEVEL_NAMES[past.risk_level],
                "assessments": [past.assessments[key] for key in sorted(past.assessments)],
            },
            "persons": {
                "total": len(past.persons),
                "by_status": by_status,
                "unaccounted_person_ids": [
                    person.person_id for person in past.persons.values() if person.status != PersonStatus.SAFE
                ],
            },
            "routes": {route_id: route.status for route_id, route in sorted(past.routes.items())},
            "teams": {
                team_id: {
                    "status": "deployed" if team.active_deployment_id else "standby",
                    "active_deployment_id": team.active_deployment_id,
                }
                for team_id, team in sorted(past.teams.items())
            },
            "materials": {batch_id: past.batch_balance(batch_id) for batch_id in sorted(past.batches)},
            "approvals": approvals,
        }

    # ------------------------------------------------------------------
    # 基础资料登记（点区清单、责任网格、分组、路线、安置点、队伍、物资）
    # ------------------------------------------------------------------

    def _register(self, key: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_mutable()
        duplicated = self._check_idem(key, payload, payload.keys())
        if duplicated is not None:
            return duplicated
        event = self._append(event_type, payload)
        return {"duplicated": False, "event_seq": event.seq}

    def register_grid(self, grid_id: str, name: str, member_ids: list[str]) -> dict[str, Any]:
        return self._register(
            f"grid:{grid_id}", "grid_registered", {"grid_id": grid_id, "name": name, "member_ids": list(member_ids)}
        )

    def register_zone(self, zone_id: str, kind: str, name: str, grid_id: str) -> dict[str, Any]:
        if kind not in ZONE_KINDS:
            raise InvalidStateError(f"未知的点区类型：{kind}")
        self._grid(grid_id)
        return self._register(
            f"zone:{zone_id}",
            "zone_registered",
            {"zone_id": zone_id, "kind": kind, "name": name, "grid_id": grid_id},
        )

    def register_route(self, route_id: str, origin_zone_id: str, shelter_id: str, primary: bool = False) -> dict[str, Any]:
        self._zone(origin_zone_id)
        self._shelter(shelter_id)
        return self._register(
            f"route:{route_id}",
            "route_registered",
            {
                "route_id": route_id,
                "origin_zone_id": origin_zone_id,
                "shelter_id": shelter_id,
                "primary": bool(primary),
            },
        )

    def register_shelter(self, shelter_id: str, name: str, capacity: int) -> dict[str, Any]:
        if not _positive_int(capacity):
            raise InvalidStateError("安置点容量必须为正整数")
        return self._register(
            f"shelter:{shelter_id}",
            "shelter_registered",
            {"shelter_id": shelter_id, "name": name, "capacity": capacity},
        )

    def register_group(self, group_id: str, kind: str, grid_id: str, zone_id: str) -> dict[str, Any]:
        if kind not in GROUP_KINDS:
            raise InvalidStateError(f"未知的分组类型：{kind}")
        self._grid(grid_id)
        self._zone(zone_id)
        return self._register(
            f"group:{group_id}",
            "group_registered",
            {"group_id": group_id, "kind": kind, "grid_id": grid_id, "zone_id": zone_id},
        )

    def register_person(
        self, person_id: str, group_id: str, name: str, phone: str, vulnerable: bool = False
    ) -> dict[str, Any]:
        self._group(group_id)
        return self._register(
            f"person:{person_id}",
            "person_registered",
            {
                "person_id": person_id,
                "group_id": group_id,
                "name": name,
                "phone": phone,
                "vulnerable": bool(vulnerable),
            },
        )

    def register_team(self, team_id: str, name: str, home_district: str, size: int) -> dict[str, Any]:
        if not _positive_int(size):
            raise InvalidStateError("队伍人数必须为正整数")
        return self._register(
            f"team:{team_id}",
            "team_registered",
            {"team_id": team_id, "name": name, "home_district": home_district, "size": size},
        )

    def register_material(self, batch_id: str, kind: str, quantity: int, location_district: str) -> dict[str, Any]:
        if not _positive_int(quantity):
            raise InvalidStateError("物资数量必须为正整数")
        return self._register(
            f"batch:{batch_id}",
            "material_registered",
            {
                "batch_id": batch_id,
                "kind": kind,
                "quantity": quantity,
                "location_district": location_district,
            },
        )

    # ------------------------------------------------------------------
    # 雨情与风险研判
    # ------------------------------------------------------------------

    def record_rainfall(
        self, rainfall_id: str, station_id: str, observed_at: datetime, amount_mm: float, by: Principal
    ) -> dict[str, Any]:
        """记录雨情。迟到雨情只影响后续措施，不改写已发出的命令。"""
        self._require_active()
        observed = _aware(observed_at)
        payload = {
            "rainfall_id": rainfall_id,
            "station_id": station_id,
            "observed_at": observed.isoformat(),
            "amount_mm": amount_mm,
            "by": by.user_id,
        }
        duplicated = self._check_idem(
            f"rainfall:{rainfall_id}", payload, ("rainfall_id", "station_id", "observed_at", "amount_mm")
        )
        if duplicated is not None:
            return duplicated
        late = (
            self.last_assessment_recorded_at is not None
            and observed < self._parse(self.last_assessment_recorded_at)
        )
        payload["late"] = late
        event = self._append("rainfall_recorded", payload)
        return {"duplicated": False, "event_seq": event.seq, "late": late}

    def assess_risk(
        self,
        assessment_id: str,
        by: Principal,
        level: int,
        scope_zone_ids: list[str],
        basis: dict[str, Any],
        order_issuer_id: str | None = None,
    ) -> dict[str, Any]:
        """风险研判。风险升级时按受影响范围生成核查与转移任务。"""
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        if not isinstance(level, int) or isinstance(level, bool) or not MIN_RISK_LEVEL <= level <= MAX_RISK_LEVEL:
            raise InvalidStateError("风险等级无效")
        zones = [self._zone(zone_id) for zone_id in scope_zone_ids]
        for rainfall_id in basis.get("rainfall_ids", []):
            if rainfall_id not in self.rainfall:
                raise NotFoundError(f"研判依据引用了不存在的雨情：{rainfall_id}")
        payload = {
            "assessment_id": assessment_id,
            "level": level,
            "scope_zone_ids": list(scope_zone_ids),
            "basis": dict(basis),
            "by": by.user_id,
        }
        duplicated = self._check_idem(f"assessment:{assessment_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        escalation = level > self.risk_level
        payload["escalation"] = escalation
        event = self._append("risk_assessed", payload)
        result: dict[str, Any] = {
            "duplicated": False,
            "event_seq": event.seq,
            "escalation": escalation,
            "verify_task_ids": [],
            "order_id": None,
            "transfer_task_ids": [],
            "skipped_group_ids": [],
        }
        if not escalation:
            return result
        for zone in zones:
            task_id = f"verify:{assessment_id}:{zone.zone_id}"
            self._append(
                "task_created",
                {
                    "task_id": task_id,
                    "kind": TaskKind.VERIFY,
                    "status": TaskStatus.PENDING,
                    "zone_id": zone.zone_id,
                    "grid_id": zone.grid_id,
                    "created_by": by.user_id,
                    "origin": assessment_id,
                },
            )
            result["verify_task_ids"].append(task_id)
        groups = [group for group in self.groups.values() if group.zone_id in set(scope_zone_ids)]
        movable = [group for group in groups if not self._active_transfer_task(group.group_id)]
        result["skipped_group_ids"] = sorted(group.group_id for group in groups if group not in movable)
        if movable:
            order_id = f"order:{assessment_id}"
            issuer = order_issuer_id or by.user_id
            self._append(
                "transfer_ordered",
                {
                    "order_id": order_id,
                    "issuer": issuer,
                    "basis_assessment_id": assessment_id,
                    "generated": True,
                },
            )
            result["order_id"] = order_id
            for group in sorted(movable, key=lambda item: item.group_id):
                needed = len(self._unaccounted(group.group_id))
                route_id, shelter_id, status = self._assign_route(group.zone_id, needed)
                task_id = f"transfer:{order_id}:{group.group_id}"
                self._append(
                    "task_created",
                    {
                        "task_id": task_id,
                        "kind": TaskKind.TRANSFER,
                        "status": status,
                        "zone_id": group.zone_id,
                        "grid_id": group.grid_id,
                        "group_id": group.group_id,
                        "route_id": route_id,
                        "shelter_id": shelter_id,
                        "order_id": order_id,
                        "created_by": issuer,
                        "origin": assessment_id,
                    },
                )
                result["transfer_task_ids"].append(task_id)
        return result

    def _active_transfer_task(self, group_id: str) -> Task | None:
        for task in self.tasks.values():
            if (
                task.kind == TaskKind.TRANSFER
                and task.group_id == group_id
                and task.status in ACTIVE_TRANSFER_STATUSES
            ):
                return task
        return None

    def _assign_route(self, zone_id: str, needed: int) -> tuple[str | None, str | None, str]:
        """为分组挑选可用路线与安置点；没有可行方案则待复核。"""
        zone = self._zone(zone_id)
        candidates: list[str] = []
        if zone.primary_route_id:
            candidates.append(zone.primary_route_id)
        candidates.extend(
            sorted(
                route.route_id
                for route in self.routes.values()
                if route.origin_zone_id == zone_id and route.route_id not in candidates
            )
        )
        for route_id in candidates:
            route = self.routes[route_id]
            if route.status != RouteStatus.OPEN:
                continue
            shelter = self.shelters[route.shelter_id]
            if shelter.remaining < needed:
                continue
            return route.route_id, shelter.shelter_id, TaskStatus.PENDING
        return None, None, TaskStatus.NEEDS_REVIEW

    # ------------------------------------------------------------------
    # 预警叫应
    # ------------------------------------------------------------------

    def issue_alert(
        self,
        campaign_id: str,
        by: Principal,
        message: str,
        deadline: datetime,
        scope_zone_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """发起叫应：目标为范围内全部分组（灾害点与风险区同等覆盖）。"""
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        if scope_zone_ids is not None:
            for zone_id in scope_zone_ids:
                self._zone(zone_id)
        targets = sorted(
            group.group_id
            for group in self.groups.values()
            if scope_zone_ids is None or group.zone_id in set(scope_zone_ids)
        )
        if not targets:
            raise InvalidStateError("叫应范围内没有分组")
        payload = {
            "campaign_id": campaign_id,
            "message": message,
            "deadline": _aware(deadline).isoformat(),
            "scope_zone_ids": list(scope_zone_ids) if scope_zone_ids is not None else None,
            "target_group_ids": targets,
            "by": by.user_id,
        }
        duplicated = self._check_idem(f"campaign:{campaign_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        event = self._append("alert_issued", payload)
        return {"duplicated": False, "event_seq": event.seq, "target_group_ids": targets}

    def record_receipt(
        self, campaign_id: str, group_id: str, receipt_id: str, by: Principal, note: str | None = None
    ) -> dict[str, Any]:
        """登记叫应回执。重复回执保持幂等：以先到的为准。"""
        self._require_active()
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            raise NotFoundError(f"叫应不存在：{campaign_id}")
        if group_id not in campaign.target_group_ids:
            raise InvalidStateError(f"分组 {group_id} 不在叫应 {campaign_id} 范围内")
        key = f"receipt:{campaign_id}:{group_id}"
        existing = self._idem.get(key)
        if existing is not None:
            return {"duplicated": True, "event_seq": existing["event_seq"]}
        owner = self._receipt_id_owners.get(receipt_id)
        if owner is not None and owner != key:
            raise ConflictError(f"回执编号 {receipt_id} 已被其他回执使用")
        payload = {
            "receipt_id": receipt_id,
            "campaign_id": campaign_id,
            "group_id": group_id,
            "by": by.user_id,
            "note": note,
        }
        event = self._append("receipt_recorded", payload)
        return {"duplicated": False, "event_seq": event.seq}

    def overdue_alerts(self, at: datetime | None = None) -> list[dict[str, Any]]:
        """逾期叫应：超过时限仍未收齐回执的叫应及其缺口分组。"""
        moment = _aware(at) if at is not None else self._now()
        overdue = []
        for campaign in sorted(self.campaigns.values(), key=lambda item: item.campaign_id):
            if self._parse(campaign.deadline) >= moment:
                continue
            missing = [
                group_id
                for group_id in campaign.target_group_ids
                if (campaign.campaign_id, group_id) not in self.receipts
            ]
            if missing:
                overdue.append(
                    {
                        "campaign_id": campaign.campaign_id,
                        "deadline": campaign.deadline,
                        "missing_group_ids": missing,
                    }
                )
        return overdue

    # ------------------------------------------------------------------
    # 转移指令与执行
    # ------------------------------------------------------------------

    def issue_transfer_order(
        self,
        order_id: str,
        by: Principal,
        assignments: list[dict[str, str]],
        basis_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        """下达转移指令，按分组生成转移任务。发布人留痕，完成需他人确认。"""
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        if not assignments:
            raise InvalidStateError("转移指令至少包含一个分组")
        if basis_assessment_id is not None and basis_assessment_id not in self.assessments:
            raise NotFoundError(f"批准依据不存在：{basis_assessment_id}")
        payload = {
            "order_id": order_id,
            "issuer": by.user_id,
            "basis_assessment_id": basis_assessment_id,
            "generated": False,
        }
        duplicated = self._check_idem(f"order:{order_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        plan = []
        for assignment in assignments:
            group = self._group(assignment["group_id"])
            route = self._route(assignment["route_id"])
            shelter = self._shelter(assignment["shelter_id"])
            if route.origin_zone_id != group.zone_id:
                raise InvalidStateError(f"路线 {route.route_id} 不覆盖分组 {group.group_id} 所在点区")
            if route.status != RouteStatus.OPEN:
                raise InvalidStateError(f"路线 {route.route_id} 已封闭")
            if route.shelter_id != shelter.shelter_id:
                raise InvalidStateError(f"路线 {route.route_id} 不通往安置点 {shelter.shelter_id}")
            if self._active_transfer_task(group.group_id) is not None:
                raise ConflictError(f"分组 {group.group_id} 已有进行中的转移任务")
            needed = len(self._unaccounted(group.group_id))
            if shelter.remaining < needed:
                raise CapacityError(f"安置点 {shelter.shelter_id} 剩余容量不足")
            plan.append((group, route, shelter))
        self._append("transfer_ordered", payload)
        task_ids = []
        for group, route, shelter in plan:
            task_id = f"transfer:{order_id}:{group.group_id}"
            self._append(
                "task_created",
                {
                    "task_id": task_id,
                    "kind": TaskKind.TRANSFER,
                    "status": TaskStatus.PENDING,
                    "zone_id": group.zone_id,
                    "grid_id": group.grid_id,
                    "group_id": group.group_id,
                    "route_id": route.route_id,
                    "shelter_id": shelter.shelter_id,
                    "order_id": order_id,
                    "created_by": by.user_id,
                    "origin": order_id,
                },
            )
            task_ids.append(task_id)
        return {"duplicated": False, "order_id": order_id, "transfer_task_ids": task_ids}

    def report_departure(self, task_id: str, by: Principal) -> dict[str, Any]:
        self._require_active()
        task = self._task(task_id)
        if task.kind != TaskKind.TRANSFER:
            raise InvalidStateError("只有转移任务可以报告出发")
        if task.status == TaskStatus.NEEDS_REVIEW:
            raise InvalidStateError("任务待复核，不能报告出发")
        if task.status != TaskStatus.PENDING:
            return {"duplicated": True, "task_id": task_id}
        event = self._append("departure_reported", {"task_id": task_id, "by": by.user_id})
        return {"duplicated": False, "event_seq": event.seq}

    def record_arrival(
        self, task_id: str, arrival_id: str, person_ids: list[str], by: Principal
    ) -> dict[str, Any]:
        """登记到达。已到安全地点的人不得被重复计数。"""
        self._require_active()
        task = self._task(task_id)
        if task.kind != TaskKind.TRANSFER:
            raise InvalidStateError("只有转移任务可以登记到达")
        unique_ids = sorted(set(person_ids))
        if not unique_ids:
            raise InvalidStateError("到达名单不能为空")
        members = {person.person_id for person in self._group_members(task.group_id or "")}
        unknown = [person_id for person_id in unique_ids if person_id not in members]
        if unknown:
            raise InvalidStateError(f"到达人员不属于该分组：{unknown}")
        payload = {
            "arrival_id": arrival_id,
            "task_id": task_id,
            "person_ids": unique_ids,
            "shelter_id": task.shelter_id,
            "by": by.user_id,
        }
        duplicated = self._check_idem(f"arrival:{arrival_id}", payload, ("arrival_id", "task_id", "person_ids"))
        if duplicated is not None:
            return duplicated
        if task.status in TERMINAL_TASK_STATUSES or task.status == TaskStatus.NEEDS_REVIEW:
            raise InvalidStateError(f"任务状态 {task.status} 不允许登记到达")
        newly = [pid for pid in unique_ids if self.persons[pid].status != PersonStatus.SAFE]
        already = [pid for pid in unique_ids if self.persons[pid].status == PersonStatus.SAFE]
        shelter = self._shelter(task.shelter_id or "")
        if shelter.remaining < len(newly):
            raise CapacityError(f"安置点 {shelter.shelter_id} 剩余容量不足")
        payload["person_ids_new"] = newly
        payload["person_ids_already"] = already
        event = self._append("arrival_recorded", payload)
        return {
            "duplicated": False,
            "event_seq": event.seq,
            "newly_safe": newly,
            "already_safe": already,
            "task_status": self.tasks[task_id].status,
        }

    def report_refusal(self, task_id: str, refusal_id: str, by: Principal, reason: str) -> dict[str, Any]:
        """拒绝转移：保留事实，并生成劝离替代任务。"""
        self._require_active()
        task = self._task(task_id)
        if task.kind != TaskKind.TRANSFER:
            raise InvalidStateError("只有转移任务可以报告拒绝")
        payload = {"refusal_id": refusal_id, "task_id": task_id, "reason": reason, "by": by.user_id}
        duplicated = self._check_idem(f"refusal:{refusal_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        if task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.NEEDS_REVIEW):
            raise InvalidStateError(f"任务状态 {task.status} 不允许报告拒绝")
        self._append("refusal_reported", payload)
        follow_up_id = f"persuade:{refusal_id}"
        self._append(
            "task_created",
            {
                "task_id": follow_up_id,
                "kind": TaskKind.PERSUADE,
                "status": TaskStatus.PENDING,
                "zone_id": task.zone_id,
                "grid_id": task.grid_id,
                "group_id": task.group_id,
                "created_by": by.user_id,
                "origin": refusal_id,
            },
        )
        return {"duplicated": False, "follow_up_task_id": follow_up_id}

    def report_comm_outage(self, outage_id: str, group_id: str, by: Principal, note: str | None = None) -> dict[str, Any]:
        """通信中断：保留事实，并生成上门核查替代任务。"""
        self._require_active()
        group = self._group(group_id)
        payload = {"outage_id": outage_id, "group_id": group_id, "note": note, "by": by.user_id}
        duplicated = self._check_idem(f"outage:{outage_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        self._append("comm_outage_reported", payload)
        follow_up_id = f"door:{outage_id}"
        self._append(
            "task_created",
            {
                "task_id": follow_up_id,
                "kind": TaskKind.DOOR_CHECK,
                "status": TaskStatus.PENDING,
                "zone_id": group.zone_id,
                "grid_id": group.grid_id,
                "group_id": group.group_id,
                "created_by": by.user_id,
                "origin": outage_id,
            },
        )
        return {"duplicated": False, "follow_up_task_id": follow_up_id}

    # ------------------------------------------------------------------
    # 路线封闭与替代方案
    # ------------------------------------------------------------------

    def close_route(self, route_id: str, by: Principal, reason: str) -> dict[str, Any]:
        """封闭路线：保留事实，并为受影响任务安排替代路线或标记待复核。"""
        self._require_active()
        route = self._route(route_id)
        if route.status != RouteStatus.OPEN:
            raise InvalidStateError(f"路线 {route_id} 已经封闭")
        self._append("route_closed", {"route_id": route_id, "reason": reason, "by": by.user_id})
        reassigned: list[str] = []
        flagged: list[str] = []
        affected = sorted(
            (
                task
                for task in self.tasks.values()
                if task.kind == TaskKind.TRANSFER
                and task.route_id == route_id
                and task.status in ACTIVE_TRANSFER_STATUSES
            ),
            key=lambda task: task.task_id,
        )
        for task in affected:
            group = self._group(task.group_id or "")
            needed = len(self._unaccounted(group.group_id))
            alt_route_id, alt_shelter_id, status = self._assign_route(group.zone_id, needed)
            if status == TaskStatus.PENDING and alt_route_id is not None:
                self._append(
                    "alternative_route_assigned",
                    {
                        "task_id": task.task_id,
                        "from_route_id": route_id,
                        "to_route_id": alt_route_id,
                        "shelter_id": alt_shelter_id,
                        "reason": "route_closed",
                    },
                )
                reassigned.append(task.task_id)
            else:
                self._append(
                    "task_flagged_review",
                    {"task_id": task.task_id, "reason": f"route_closed:{reason}"},
                )
                flagged.append(task.task_id)
        return {"reassigned_task_ids": reassigned, "flagged_task_ids": flagged}

    def reopen_route(self, route_id: str, by: Principal) -> dict[str, Any]:
        self._require_active()
        route = self._route(route_id)
        if route.status != RouteStatus.CLOSED:
            raise InvalidStateError(f"路线 {route_id} 并未封闭")
        event = self._append("route_reopened", {"route_id": route_id, "by": by.user_id})
        return {"duplicated": False, "event_seq": event.seq}

    def review_task(
        self,
        task_id: str,
        by: Principal,
        route_id: str | None = None,
        shelter_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """复核待复核的转移任务：指定或自动选择路线与安置点后恢复执行。"""
        self._require_active()
        task = self._task(task_id)
        if task.kind != TaskKind.TRANSFER or task.status != TaskStatus.NEEDS_REVIEW:
            raise InvalidStateError("只有待复核的转移任务可以复核")
        group = self._group(task.group_id or "")
        if route_id is None:
            needed = len(self._unaccounted(group.group_id))
            route_id, shelter_id, status = self._assign_route(group.zone_id, needed)
            if status != TaskStatus.PENDING:
                raise InvalidStateError("仍无可用路线与安置点，保持待复核")
        else:
            route = self._route(route_id)
            shelter = self._shelter(shelter_id or route.shelter_id)
            if route.origin_zone_id != group.zone_id:
                raise InvalidStateError(f"路线 {route_id} 不覆盖该分组所在点区")
            if route.status != RouteStatus.OPEN:
                raise InvalidStateError(f"路线 {route_id} 已封闭")
            if route.shelter_id != shelter.shelter_id:
                raise InvalidStateError(f"路线 {route_id} 不通往安置点 {shelter.shelter_id}")
            needed = len(self._unaccounted(group.group_id))
            if shelter.remaining < needed:
                raise CapacityError(f"安置点 {shelter.shelter_id} 剩余容量不足")
            shelter_id = shelter.shelter_id
        event = self._append(
            "task_reviewed",
            {
                "task_id": task_id,
                "route_id": route_id,
                "shelter_id": shelter_id,
                "note": note,
                "by": by.user_id,
            },
        )
        return {"duplicated": False, "event_seq": event.seq, "route_id": route_id, "shelter_id": shelter_id}

    def confirm_task(self, task_id: str, by: Principal, note: str | None = None) -> dict[str, Any]:
        """确认任务完成。发布指令的人不能独自确认，须由他人确认。"""
        self._require_active()
        task = self._task(task_id)
        if by.user_id == task.created_by:
            raise SeparationOfDutiesError("发布指令的人不能独自确认任务完成")
        if task.kind == TaskKind.TRANSFER:
            if task.status != TaskStatus.AWAITING_CONFIRM:
                raise InvalidStateError("仍有人员未确认安全，不能确认任务完成")
        elif task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            raise InvalidStateError(f"任务状态 {task.status} 不允许确认")
        event = self._append(
            "task_confirmed",
            {"task_id": task_id, "confirmer": by.user_id, "note": note},
        )
        return {"duplicated": False, "event_seq": event.seq}

    # ------------------------------------------------------------------
    # 救援队伍与物资前置
    # ------------------------------------------------------------------

    def deploy_team(
        self, deployment_id: str, team_id: str, to_district: str, mission: str, by: Principal
    ) -> dict[str, Any]:
        """前置救援力量。同一队伍同时只能承担一处前置任务，跨区支援不得重复占用。"""
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        team = self._team(team_id)
        payload = {
            "deployment_id": deployment_id,
            "team_id": team_id,
            "to_district": to_district,
            "mission": mission,
            "by": by.user_id,
        }
        duplicated = self._check_idem(f"deployment:{deployment_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        if team.active_deployment_id is not None:
            raise DoubleAllocationError(f"队伍 {team_id} 已有前置任务，不能重复占用")
        payload["cross_district"] = to_district != team.home_district
        event = self._append("team_deployed", payload)
        return {"duplicated": False, "event_seq": event.seq, "cross_district": payload["cross_district"]}

    def recall_team(self, deployment_id: str, by: Principal) -> dict[str, Any]:
        self._require_active()
        self._require_role(by, Role.COMMANDER)
        deployment = self.deployments.get(deployment_id)
        if deployment is None:
            raise NotFoundError(f"前置任务不存在：{deployment_id}")
        if not deployment.active:
            raise InvalidStateError(f"前置任务 {deployment_id} 已结束")
        event = self._append("team_recalled", {"deployment_id": deployment_id, "by": by.user_id})
        return {"duplicated": False, "event_seq": event.seq}

    def hold_material(
        self, hold_id: str, batch_id: str, quantity: int, purpose: str, by: Principal
    ) -> dict[str, Any]:
        """预占物资。可用量 = 总量 - 预占 - 在途 - 已交接，不得超占。"""
        self._require_active()
        self._require_role(by, Role.LOGISTICS, Role.COMMANDER)
        batch = self._batch(batch_id)
        if not _positive_int(quantity):
            raise InvalidStateError("预占数量必须为正整数")
        payload = {
            "hold_id": hold_id,
            "batch_id": batch_id,
            "quantity": quantity,
            "purpose": purpose,
            "by": by.user_id,
        }
        duplicated = self._check_idem(f"hold:{hold_id}", payload, payload.keys())
        if duplicated is not None:
            return duplicated
        if self.batch_balance(batch_id)["available"] < quantity:
            raise InsufficientStockError(f"物资批次 {batch_id} 可用量不足")
        event = self._append("material_held", payload)
        return {"duplicated": False, "event_seq": event.seq}

    def dispatch_hold(self, hold_id: str, by: Principal) -> dict[str, Any]:
        """领取出库：预占转为在途。重复领取保持幂等。"""
        self._require_active()
        self._require_role(by, Role.LOGISTICS, Role.COMMANDER)
        hold = self._hold(hold_id)
        if hold.status == HoldStatus.HELD:
            event = self._append("material_dispatched", {"hold_id": hold_id, "by": by.user_id})
            return {"duplicated": False, "event_seq": event.seq}
        if hold.status == HoldStatus.IN_TRANSIT:
            return {"duplicated": True, "hold_id": hold_id}
        raise InvalidStateError(f"预占状态 {hold.status} 不允许出库")

    def receive_hold(self, hold_id: str, by: Principal) -> dict[str, Any]:
        """交接完成：在途物资落地，库存交接闭环。重复交接保持幂等。"""
        self._require_active()
        self._require_role(by, Role.LOGISTICS, Role.COMMANDER)
        hold = self._hold(hold_id)
        if hold.status == HoldStatus.IN_TRANSIT:
            event = self._append("material_received", {"hold_id": hold_id, "by": by.user_id})
            return {"duplicated": False, "event_seq": event.seq}
        if hold.status == HoldStatus.RECEIVED:
            return {"duplicated": True, "hold_id": hold_id}
        if hold.status == HoldStatus.HELD:
            raise InvalidStateError("物资尚未领取出库，不能交接")
        raise InvalidStateError(f"预占状态 {hold.status} 不允许交接")

    def release_hold(self, hold_id: str, by: Principal) -> dict[str, Any]:
        """解除预占，数量回到可用量。"""
        self._require_active()
        self._require_role(by, Role.LOGISTICS, Role.COMMANDER)
        hold = self._hold(hold_id)
        if hold.status == HoldStatus.HELD:
            event = self._append("material_released", {"hold_id": hold_id, "by": by.user_id})
            return {"duplicated": False, "event_seq": event.seq}
        if hold.status == HoldStatus.RELEASED:
            return {"duplicated": True, "hold_id": hold_id}
        raise InvalidStateError("物资已出库，不能解除预占，应办理交接")

    def batch_balance(self, batch_id: str) -> dict[str, int]:
        batch = self._batch(batch_id)
        held = in_transit = received = 0
        for hold in self.holds.values():
            if hold.batch_id != batch_id:
                continue
            if hold.status == HoldStatus.HELD:
                held += hold.quantity
            elif hold.status == HoldStatus.IN_TRANSIT:
                in_transit += hold.quantity
            elif hold.status == HoldStatus.RECEIVED:
                received += hold.quantity
        return {
            "quantity": batch.quantity,
            "held": held,
            "in_transit": in_transit,
            "received": received,
            "available": batch.quantity - held - in_transit - received,
        }

    # ------------------------------------------------------------------
    # 查询与交班视图
    # ------------------------------------------------------------------

    def unaccounted_persons(self) -> list[dict[str, Any]]:
        """尚未确认安全的人员清单（不含敏感信息）。"""
        result = []
        for person in sorted(self.persons.values(), key=lambda item: item.person_id):
            if person.status == PersonStatus.SAFE:
                continue
            group = self.groups[person.group_id]
            result.append(
                {
                    "person_id": person.person_id,
                    "group_id": person.group_id,
                    "grid_id": group.grid_id,
                    "status": person.status,
                }
            )
        return result

    def pending_review_transfers(self) -> list[str]:
        """待复核转移任务。"""
        return sorted(
            task.task_id
            for task in self.tasks.values()
            if task.kind == TaskKind.TRANSFER and task.status == TaskStatus.NEEDS_REVIEW
        )

    def pending_handover_holds(self) -> list[str]:
        """库存交接在途：已领取出库、尚未交接完成的预占。"""
        return sorted(hold.hold_id for hold in self.holds.values() if hold.status == HoldStatus.IN_TRANSIT)

    def handover_report(self) -> dict[str, Any]:
        """交班视图：一眼看清还有谁未确认安全、哪些事项待续办。"""
        return {
            "response_id": (self.response or {}).get("response_id"),
            "risk_level": self.risk_level,
            "risk_level_name": RISK_LEVEL_NAMES[self.risk_level],
            "overdue_alerts": self.overdue_alerts(),
            "unaccounted_persons": self.unaccounted_persons(),
            "pending_review_transfers": self.pending_review_transfers(),
            "pending_handover_holds": self.pending_handover_holds(),
            "active_deployments": [
                {
                    "deployment_id": deployment.deployment_id,
                    "team_id": deployment.team_id,
                    "to_district": deployment.to_district,
                }
                for deployment in sorted(self.deployments.values(), key=lambda item: item.deployment_id)
                if deployment.active
            ],
        }

    def group_roster(self, principal: Principal, group_id: str) -> list[dict[str, Any]]:
        """分组名册。敏感人员信息只向相应网格和安置岗位开放。"""
        group = self._group(group_id)
        members = self._group_members(group_id)
        if self._can_view_pii(principal, group, members):
            return [self._person_view(person, masked=False) for person in members]
        return [self._person_view(person, masked=True) for person in members]

    def _can_view_pii(self, principal: Principal, group: Group, members: list[Person]) -> bool:
        if principal.role == Role.GRID_MEMBER and principal.grid_id == group.grid_id:
            return True
        if principal.role == Role.SHELTER_STAFF and principal.shelter_id:
            shelter_id = principal.shelter_id
            if any(person.shelter_id == shelter_id for person in members):
                return True
            for task in self.tasks.values():
                if (
                    task.kind == TaskKind.TRANSFER
                    and task.group_id == group.group_id
                    and task.shelter_id == shelter_id
                    and task.status not in TERMINAL_TASK_STATUSES
                ):
                    return True
        return False

    @staticmethod
    def _person_view(person: Person, masked: bool) -> dict[str, Any]:
        return {
            "person_id": person.person_id,
            "name": mask_name(person.name) if masked else person.name,
            "phone": mask_phone(person.phone) if masked else person.phone,
            "vulnerable": person.vulnerable,
            "status": person.status,
            "shelter_id": person.shelter_id,
        }

    # ------------------------------------------------------------------
    # 事件回放：把事实应用到状态
    # ------------------------------------------------------------------

    def _on_response_activated(self, payload: dict[str, Any], event: Event) -> None:
        self.response = payload

    def _on_response_deactivated(self, payload: dict[str, Any], event: Event) -> None:
        self.deactivated = payload
        self._remember(f"deactivation:{payload['deactivation_id']}", payload, event)

    def _on_grid_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.grids[payload["grid_id"]] = Grid(
            grid_id=payload["grid_id"], name=payload["name"], member_ids=list(payload["member_ids"])
        )
        self._remember(f"grid:{payload['grid_id']}", payload, event)

    def _on_zone_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.zones[payload["zone_id"]] = Zone(
            zone_id=payload["zone_id"], kind=payload["kind"], name=payload["name"], grid_id=payload["grid_id"]
        )
        self._remember(f"zone:{payload['zone_id']}", payload, event)

    def _on_route_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.routes[payload["route_id"]] = Route(
            route_id=payload["route_id"],
            origin_zone_id=payload["origin_zone_id"],
            shelter_id=payload["shelter_id"],
        )
        if payload.get("primary"):
            zone = self.zones[payload["origin_zone_id"]]
            zone.primary_route_id = payload["route_id"]
            zone.primary_shelter_id = payload["shelter_id"]
        self._remember(f"route:{payload['route_id']}", payload, event)

    def _on_shelter_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.shelters[payload["shelter_id"]] = Shelter(
            shelter_id=payload["shelter_id"], name=payload["name"], capacity=payload["capacity"]
        )
        self._remember(f"shelter:{payload['shelter_id']}", payload, event)

    def _on_group_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.groups[payload["group_id"]] = Group(
            group_id=payload["group_id"],
            kind=payload["kind"],
            grid_id=payload["grid_id"],
            zone_id=payload["zone_id"],
        )
        self._remember(f"group:{payload['group_id']}", payload, event)

    def _on_person_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.persons[payload["person_id"]] = Person(
            person_id=payload["person_id"],
            group_id=payload["group_id"],
            name=payload["name"],
            phone=payload["phone"],
            vulnerable=payload["vulnerable"],
        )
        self._remember(f"person:{payload['person_id']}", payload, event)

    def _on_team_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.teams[payload["team_id"]] = Team(
            team_id=payload["team_id"],
            name=payload["name"],
            home_district=payload["home_district"],
            size=payload["size"],
        )
        self._remember(f"team:{payload['team_id']}", payload, event)

    def _on_material_registered(self, payload: dict[str, Any], event: Event) -> None:
        self.batches[payload["batch_id"]] = Batch(
            batch_id=payload["batch_id"],
            kind=payload["kind"],
            quantity=payload["quantity"],
            location_district=payload["location_district"],
        )
        self._remember(f"batch:{payload['batch_id']}", payload, event)

    def _on_rainfall_recorded(self, payload: dict[str, Any], event: Event) -> None:
        self.rainfall[payload["rainfall_id"]] = payload
        self._remember(f"rainfall:{payload['rainfall_id']}", payload, event)

    def _on_risk_assessed(self, payload: dict[str, Any], event: Event) -> None:
        self.risk_level = payload["level"]
        self.assessments[payload["assessment_id"]] = payload
        self.last_assessment_recorded_at = event.recorded_at
        self._remember(f"assessment:{payload['assessment_id']}", payload, event)

    def _on_alert_issued(self, payload: dict[str, Any], event: Event) -> None:
        self.campaigns[payload["campaign_id"]] = Campaign(
            campaign_id=payload["campaign_id"],
            message=payload["message"],
            deadline=payload["deadline"],
            scope_zone_ids=payload["scope_zone_ids"],
            target_group_ids=list(payload["target_group_ids"]),
            issued_by=payload["by"],
        )
        self._remember(f"campaign:{payload['campaign_id']}", payload, event)

    def _on_receipt_recorded(self, payload: dict[str, Any], event: Event) -> None:
        key = (payload["campaign_id"], payload["group_id"])
        self.receipts[key] = Receipt(
            receipt_id=payload["receipt_id"],
            campaign_id=payload["campaign_id"],
            group_id=payload["group_id"],
            reported_by=payload["by"],
            note=payload["note"],
        )
        idem_key = f"receipt:{payload['campaign_id']}:{payload['group_id']}"
        self._remember(idem_key, payload, event)
        self._receipt_id_owners[payload["receipt_id"]] = idem_key

    def _on_transfer_ordered(self, payload: dict[str, Any], event: Event) -> None:
        self.orders[payload["order_id"]] = Order(
            order_id=payload["order_id"],
            issuer=payload["issuer"],
            basis_assessment_id=payload.get("basis_assessment_id"),
            generated=payload["generated"],
        )
        self._remember(f"order:{payload['order_id']}", payload, event)

    def _on_task_created(self, payload: dict[str, Any], event: Event) -> None:
        self.tasks[payload["task_id"]] = Task(
            task_id=payload["task_id"],
            kind=payload["kind"],
            status=payload["status"],
            grid_id=payload["grid_id"],
            created_by=payload["created_by"],
            zone_id=payload.get("zone_id"),
            group_id=payload.get("group_id"),
            route_id=payload.get("route_id"),
            shelter_id=payload.get("shelter_id"),
            order_id=payload.get("order_id"),
            origin=payload.get("origin"),
        )
        self._remember(f"task:{payload['task_id']}", payload, event)

    def _on_departure_reported(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.status = TaskStatus.IN_PROGRESS
        for person in self._group_members(task.group_id or ""):
            if person.status in (PersonStatus.AT_RISK, PersonStatus.UNREACHABLE):
                person.status = PersonStatus.EN_ROUTE

    def _on_arrival_recorded(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        shelter = self.shelters[payload["shelter_id"]]
        for person_id in payload["person_ids_new"]:
            person = self.persons[person_id]
            person.status = PersonStatus.SAFE
            person.shelter_id = shelter.shelter_id
        shelter.occupancy += len(payload["person_ids_new"])
        members = self._group_members(task.group_id or "")
        if members and all(person.status == PersonStatus.SAFE for person in members):
            task.status = TaskStatus.AWAITING_CONFIRM
        self._remember(f"arrival:{payload['arrival_id']}", payload, event)

    def _on_refusal_reported(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.status = TaskStatus.REFUSED
        for person in self._group_members(task.group_id or ""):
            if person.status != PersonStatus.SAFE:
                person.status = PersonStatus.REFUSED
        self._remember(f"refusal:{payload['refusal_id']}", payload, event)

    def _on_comm_outage_reported(self, payload: dict[str, Any], event: Event) -> None:
        for person in self._group_members(payload["group_id"]):
            if person.status != PersonStatus.SAFE:
                person.status = PersonStatus.UNREACHABLE
        self._remember(f"outage:{payload['outage_id']}", payload, event)

    def _on_route_closed(self, payload: dict[str, Any], event: Event) -> None:
        route = self.routes[payload["route_id"]]
        route.status = RouteStatus.CLOSED
        route.closed_reason = payload["reason"]

    def _on_route_reopened(self, payload: dict[str, Any], event: Event) -> None:
        route = self.routes[payload["route_id"]]
        route.status = RouteStatus.OPEN
        route.closed_reason = None

    def _on_alternative_route_assigned(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.route_id = payload["to_route_id"]
        task.shelter_id = payload["shelter_id"]
        if task.status == TaskStatus.NEEDS_REVIEW:
            task.status = TaskStatus.PENDING

    def _on_task_flagged_review(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.status = TaskStatus.NEEDS_REVIEW
        task.note = payload["reason"]

    def _on_task_reviewed(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.route_id = payload["route_id"]
        task.shelter_id = payload["shelter_id"]
        task.status = TaskStatus.PENDING
        task.note = payload.get("note")

    def _on_task_confirmed(self, payload: dict[str, Any], event: Event) -> None:
        task = self.tasks[payload["task_id"]]
        task.status = TaskStatus.CONFIRMED
        task.confirmed_by = payload["confirmer"]

    def _on_team_deployed(self, payload: dict[str, Any], event: Event) -> None:
        self.deployments[payload["deployment_id"]] = Deployment(
            deployment_id=payload["deployment_id"],
            team_id=payload["team_id"],
            to_district=payload["to_district"],
            mission=payload["mission"],
            issued_by=payload["by"],
            cross_district=payload["cross_district"],
        )
        self.teams[payload["team_id"]].active_deployment_id = payload["deployment_id"]
        self._remember(f"deployment:{payload['deployment_id']}", payload, event)

    def _on_team_recalled(self, payload: dict[str, Any], event: Event) -> None:
        deployment = self.deployments[payload["deployment_id"]]
        deployment.active = False
        self.teams[deployment.team_id].active_deployment_id = None

    def _on_material_held(self, payload: dict[str, Any], event: Event) -> None:
        self.holds[payload["hold_id"]] = Hold(
            hold_id=payload["hold_id"],
            batch_id=payload["batch_id"],
            quantity=payload["quantity"],
            purpose=payload["purpose"],
        )
        self._remember(f"hold:{payload['hold_id']}", payload, event)

    def _on_material_dispatched(self, payload: dict[str, Any], event: Event) -> None:
        self.holds[payload["hold_id"]].status = HoldStatus.IN_TRANSIT

    def _on_material_received(self, payload: dict[str, Any], event: Event) -> None:
        self.holds[payload["hold_id"]].status = HoldStatus.RECEIVED

    def _on_material_released(self, payload: dict[str, Any], event: Event) -> None:
        self.holds[payload["hold_id"]].status = HoldStatus.RELEASED
