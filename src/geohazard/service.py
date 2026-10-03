"""地质灾害响应服务：命令处理与全部业务不变量。

所有写操作都以命令形式携带逻辑时间，成功后向只追加账本写入事件；
校验失败抛 GeoHazardError，账本保持不变。核心不变量：

1. 迟到雨情可以记录并改变后续研判与措施，但只能产生新事件，
   已发出的命令事件不可改写（账本只追加）。
2. 人员按唯一 person_id 计一次：重复叫应回执幂等返回，重复到达/入住
   不增加安置点人数。
3. 拒绝转移、通信中断、路线封闭必须作为事实保留，并立即开立替代方案；
   任务在全员"有着落"（安全入住或替代方案在办）前不能被双岗确认完成。
4. 发布指令者本人不能独自确认任务完成，须第二名不同岗位人员会签。
5. 物资先预留后领用：可用量=库存-预留；跨区支援物资在源端冻结，
   不会两地重复占用；库存交接待接收期间冻结出库与调动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import access
from .errors import GeoHazardError, IdempotentReplay, NotFoundError, PermissionDeniedError
from .events import EventStore
from .projections import Projection, RISK_ORDER
from .timeutil import earlier

TRANSFER_LEVELS = {"orange", "red"}


@dataclass
class Command:
    actor: str
    role: str
    ts: str


class ResponseService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.proj = Projection.rebuild(store.replay())

    # ---- 内部工具 ----
    def _append(self, cmd: Command, etype: str, payload: dict, **kw: Any):
        evt = self.store.append(
            ts=cmd.ts,
            actor=cmd.actor,
            role=cmd.role,
            etype=etype,
            payload=payload,
            **kw,
        )
        self.proj.apply(evt)
        return evt

    def _require(self, role: str, *allowed: str) -> None:
        access.require_role(role, *allowed)

    def _get(self, table: dict, key: str, label: str):
        if key not in table:
            raise NotFoundError(f"{label}不存在: {key}")
        return table[key]

    def _must_active_response(self) -> dict:
        resp = self.proj.active_response()
        if resp is None:
            raise GeoHazardError("当前没有进行中的响应")
        return resp

    # ---- 响应生命周期 ----
    def open_response(
        self, cmd: Command, response_id: str, level: int, region: str, basis: str
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        if response_id in self.proj.responses:
            raise GeoHazardError("响应编号已存在")
        if self.proj.active_response() is not None:
            raise GeoHazardError("已有进行中的响应，不能重复启动")
        if level != 4:
            raise GeoHazardError("当前服务按国家地质灾害四级应急响应启动")
        evt = self._append(
            cmd,
            "ResponseOpened",
            {"response_id": response_id, "level": level, "region": region, "basis": basis},
            command_id=response_id,
            basis=basis,
        )
        return {"event_seq": evt.seq, "response_id": response_id}

    def approve_closure(self, cmd: Command, response_id: str, note: str = "") -> dict:
        self._require(cmd.role, access.COMMANDER)
        resp = self._get(self.proj.responses, response_id, "响应")
        if resp["status"] != "active":
            raise GeoHazardError("响应不在进行中")
        prior = self.proj.approvals.get(response_id, [])
        if any(a["approver"] == cmd.actor for a in prior):
            raise GeoHazardError("同一批准人不得重复批准")
        if not prior and cmd.actor == resp["opened_by"]:
            raise GeoHazardError("第一名批准人不能是响应启动者本人")
        evt = self._append(
            cmd,
            "ResponseClosureApproved",
            {"response_id": response_id, "note": note},
        )
        return {"event_seq": evt.seq, "approvals": len(prior) + 1}

    def close_response(self, cmd: Command, response_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER)
        resp = self._get(self.proj.responses, response_id, "响应")
        if resp["status"] != "active":
            raise GeoHazardError("响应已解除")
        open_tasks = [t["task_id"] for t in self.proj.tasks.values() if t["status"] == "open"]
        if open_tasks:
            raise GeoHazardError(f"仍有未闭环任务: {','.join(sorted(open_tasks))}")
        approvals = self.proj.approvals.get(response_id, [])
        if len(approvals) < 2:
            raise GeoHazardError("解除响应须两名指挥人员批准")
        if not any(a["approver"] != resp["opened_by"] for a in approvals):
            raise GeoHazardError("批准人必须包含非响应启动者")
        snapshot = self._snapshot()
        evt = self._append(
            cmd,
            "ResponseClosed",
            {"response_id": response_id, "snapshot": snapshot},
            command_id=response_id,
        )
        return {"event_seq": evt.seq, "response_id": response_id, "snapshot": snapshot}

    def _snapshot(self) -> dict:
        """解除时点快照：风险、人员、路线、力量、批准依据全部留痕。"""
        return {
            "last_ts": self.proj.last_ts,
            "assessments": [
                {
                    "id": a["assessment_id"],
                    "level": a["level"],
                    "zones": a["zone_ids"],
                    "basis_reports": a["basis_reports"],
                }
                for a in self.proj.assessments.values()
            ],
            "people": {
                pid: {
                    "status": p["status"],
                    "verified": p["verified"],
                    "arrival_shelter": p["arrival_shelter"],
                    "group_id": p["group_id"],
                }
                for pid, p in self.proj.people.items()
            },
            "routes": {
                rid: {"status": r["status"], "shelter_id": r["shelter_id"]}
                for rid, r in self.proj.routes.items()
            },
            "teams": {
                tid: {"status": t["status"], "position": t["position"]}
                for tid, t in self.proj.teams.items()
            },
            "stock": {
                loc: dict(batches) for loc, batches in self.proj.stock.items()
            },
        }

    # ---- 雨情与风险研判 ----
    def record_rain_report(
        self,
        cmd: Command,
        report_id: str,
        station: str,
        issued_at: str,
        cumulative_mm: float,
        intensity_mm_h: float = 0.0,
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        if report_id in self.proj.reports:
            raise GeoHazardError("雨情报告编号已存在")
        if cumulative_mm < 0 or intensity_mm_h < 0:
            raise GeoHazardError("雨量不能为负")
        late = earlier(issued_at, cmd.ts)
        evt = self._append(
            cmd,
            "RainReportRecorded",
            {
                "report_id": report_id,
                "station": station,
                "issued_at": issued_at,
                "cumulative_mm": cumulative_mm,
                "intensity_mm_h": intensity_mm_h,
                "late": late,
            },
        )
        return {"event_seq": evt.seq, "late": late}

    def issue_risk_assessment(
        self,
        cmd: Command,
        assessment_id: str,
        level: str,
        zone_ids: list[str],
        basis_reports: list[str],
        note: str = "",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        if level not in RISK_ORDER:
            raise GeoHazardError("风险等级无效")
        if not zone_ids:
            raise GeoHazardError("研判必须覆盖具体点区")
        for zid in zone_ids:
            self._get(self.proj.zones, zid, "风险点区")
        for rid in basis_reports:
            self._get(self.proj.reports, rid, "雨情报告")
        latest = self.proj.latest_assessment()
        if latest and RISK_ORDER[level] < RISK_ORDER[latest["level"]]:
            raise GeoHazardError("降级须走解除/重新研判流程，不能直接签发更低等级")
        evt = self._append(
            cmd,
            "RiskAssessmentIssued",
            {
                "assessment_id": assessment_id,
                "level": level,
                "zone_ids": zone_ids,
                "basis_reports": basis_reports,
                "note": note,
            },
            command_id=assessment_id,
        )
        return {"event_seq": evt.seq, "assessment_id": assessment_id}

    # ---- 基础台账 ----
    def define_grid(self, cmd: Command, grid_id: str, name: str) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID)
        evt = self._append(cmd, "GridDefined", {"grid_id": grid_id, "name": name})
        return {"event_seq": evt.seq}

    def register_zone(
        self, cmd: Command, zone_id: str, name: str, kind: str, grid_id: str,
        risk_level: str = "blue",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        self._get(self.proj.grids, grid_id, "责任网格")
        evt = self._append(
            cmd,
            "ZoneRegistered",
            {
                "zone_id": zone_id,
                "name": name,
                "kind": kind,
                "grid_id": grid_id,
                "risk_level": risk_level,
            },
        )
        return {"event_seq": evt.seq}

    def define_route(
        self, cmd: Command, route_id: str, shelter_id: str,
        zone_ids: list[str], name: str = "",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        self._get(self.proj.shelters, shelter_id, "安置点")
        evt = self._append(
            cmd,
            "RouteDefined",
            {"route_id": route_id, "shelter_id": shelter_id, "zone_ids": zone_ids, "name": name},
        )
        return {"event_seq": evt.seq}

    def close_route(self, cmd: Command, route_id: str, reason: str) -> dict:
        """路线封闭是事实：记录后自动为在途转移与待转移任务开立替代路线方案。"""
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        route = self._get(self.proj.routes, route_id, "转移路线")
        if route["status"] == "closed":
            raise GeoHazardError("路线已封闭")
        seq = self._append(cmd, "RouteClosed", {"route_id": route_id, "reason": reason}).seq
        alt_ids = []
        for task in self.proj.tasks.values():
            if task["status"] != "open" or task.get("route_id") != route_id:
                continue
            alt_id = f"ALT-ROUTE-{route_id}-{task['task_id']}"
            if alt_id not in self.proj.alternatives:
                alt = self._append(
                    cmd,
                    "AlternativePlanOpened",
                    {
                        "alt_id": alt_id,
                        "task_id": task["task_id"],
                        "reason": f"路线封闭: {reason}",
                        "measure": "改用备用路线或就近安全区",
                        "old_route_id": route_id,
                    },
                )
                alt_ids.append(alt_id)
        for person in self.proj.people.values():
            if person["status"] == "transferring" and person.get("route_id") == route_id:
                alt_id = f"ALT-ROUTE-{route_id}-P-{person['person_id']}"
                if alt_id not in self.proj.alternatives:
                    self._append(
                        cmd,
                        "AlternativePlanOpened",
                        {
                            "alt_id": alt_id,
                            "person_id": person["person_id"],
                            "reason": f"路线封闭: {reason}",
                            "measure": "救援队伍接引并改用备用路线",
                            "old_route_id": route_id,
                        },
                    )
                    alt_ids.append(alt_id)
        return {"event_seq": seq, "alternatives": alt_ids}

    def reopen_route(self, cmd: Command, route_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER)
        route = self._get(self.proj.routes, route_id, "转移路线")
        if route["status"] != "closed":
            raise GeoHazardError("路线未封闭")
        evt = self._append(cmd, "RouteReopened", {"route_id": route_id})
        return {"event_seq": evt.seq}

    def define_shelter(self, cmd: Command, shelter_id: str, name: str, capacity: int) -> dict:
        self._require(cmd.role, access.COMMANDER)
        if capacity <= 0:
            raise GeoHazardError("安置点容量必须为正")
        evt = self._append(
            cmd,
            "ShelterDefined",
            {"shelter_id": shelter_id, "name": name, "capacity": capacity},
        )
        return {"event_seq": evt.seq}

    # ---- 分组与人员（住户与游客分组） ----
    def register_group(
        self, cmd: Command, group_id: str, kind: str, zone_id: str,
        grid_id: str, name: str = "",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID)
        self._get(self.proj.zones, zone_id, "风险点区")
        self._get(self.proj.grids, grid_id, "责任网格")
        if kind not in ("resident", "tourist", "worker"):
            raise GeoHazardError("分组类型无效")
        evt = self._append(
            cmd,
            "GroupRegistered",
            {"group_id": group_id, "kind": kind, "zone_id": zone_id, "grid_id": grid_id, "name": name},
        )
        return {"event_seq": evt.seq}

    def register_person(
        self, cmd: Command, person_id: str, name: str, group_id: str,
        id_number: str = "", phone: str = "", care_need: str = "",
        kind: str = "resident",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID)
        group = self._get(self.proj.groups, group_id, "分组")
        if person_id in self.proj.people:
            raise GeoHazardError("人员已登记")
        evt = self._append(
            cmd,
            "PersonRegistered",
            {
                "person_id": person_id,
                "name": name,
                "kind": kind,
                "group_id": group_id,
                "grid_id": group["grid_id"],
                "zone_id": group["zone_id"],
                "id_number": id_number,
                "phone": phone,
                "care_need": care_need,
            },
        )
        return {"event_seq": evt.seq}

    # ---- 风险升级：按受影响范围生成核查与转移任务 ----
    def generate_tasks(self, cmd: Command, assessment_id: str, sla_minutes: int = 30) -> dict:
        self._require(cmd.role, access.COMMANDER)
        self._must_active_response()
        assess = self._get(self.proj.assessments, assessment_id, "风险研判")
        need_transfer = assess["level"] in TRANSFER_LEVELS
        created, escalated = [], []
        for zid in assess["zone_ids"]:
            zone = self.proj.zones[zid]
            group_ids = sorted(
                g["group_id"] for g in self.proj.groups.values() if g["zone_id"] == zid
            )
            existing = next(
                (t for t in self.proj.tasks.values() if t["zone_id"] == zid and t["status"] == "open"),
                None,
            )
            if existing is not None:
                add = [k for k in (["transfer"] if need_transfer else []) if k not in existing["kinds"]]
                if add or RISK_ORDER[assess["level"]] > RISK_ORDER[existing["level"]]:
                    self._append(
                        cmd,
                        "TaskEscalated",
                        {
                            "task_id": existing["task_id"],
                            "add_kinds": add,
                            "level": assess["level"],
                        },
                        idem_key=f"{assessment_id}:{zid}",
                    )
                    escalated.append(existing["task_id"])
                continue
            route_id = next(
                (rid for rid, r in self.proj.routes.items()
                 if r["status"] == "open" and zid in r["zone_ids"]),
                "",
            )
            task_id = f"TASK-{zid}"
            kinds = ["verify", "warn"] + (["transfer"] if need_transfer else [])
            self._append(
                cmd,
                "TaskGenerated",
                {
                    "task_id": task_id,
                    "assessment_id": assessment_id,
                    "zone_id": zid,
                    "grid_id": zone["grid_id"],
                    "kinds": kinds,
                    "group_ids": group_ids,
                    "route_id": route_id,
                    "shelter_id": self.proj.routes[route_id]["shelter_id"] if route_id else "",
                    "level": assess["level"],
                    "sla_minutes": sla_minutes,
                },
                idem_key=f"{assessment_id}:{zid}",
                basis=assessment_id,
            )
            created.append(task_id)
            if not route_id and need_transfer:
                self._append(
                    cmd,
                    "AlternativePlanOpened",
                    {
                        "alt_id": f"ALT-NOROUTE-{zid}",
                        "task_id": task_id,
                        "reason": "受影响区域无可用开放转移路线",
                        "measure": "开辟备用路线或预置队伍上门组织就近避险",
                    },
                )
        return {"assessment_id": assessment_id, "created": created, "escalated": escalated}

    def reroute_task(
        self, cmd: Command, task_id: str, new_route_id: str, reason: str = "",
    ) -> dict:
        """路线封闭后的正式改道：产生事件，重放后任务仍挂备用路线。"""
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        task = self._get(self.proj.tasks, task_id, "任务")
        new_route = self._get(self.proj.routes, new_route_id, "备用路线")
        if new_route["status"] != "open":
            raise GeoHazardError("备用路线未开放")
        if task["zone_id"] not in new_route["zone_ids"]:
            raise GeoHazardError("备用路线不覆盖该任务点区")
        old_route_id = task.get("route_id", "")
        evt = self._append(
            cmd,
            "TaskRouteRerouted",
            {
                "task_id": task_id,
                "old_route_id": old_route_id,
                "new_route_id": new_route_id,
                "reason": reason,
            },
        )
        return {"event_seq": evt.seq, "route_id": new_route_id}

    # ---- 叫应 ----
    def attempt_call(
        self, cmd: Command, task_id: str, person_id: str, channel: str, result: str,
    ) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID)
        self._check_task_scope(cmd, task_id)
        self._get(self.proj.people, person_id, "人员")
        evt = self._append(
            cmd,
            "CallAttempted",
            {"task_id": task_id, "person_id": person_id, "channel": channel, "result": result},
        )
        return {"event_seq": evt.seq}

    def confirm_receipt(
        self, cmd: Command, task_id: str, person_id: str, idem_key: str, channel: str = "phone",
    ) -> dict:
        """叫应回执：同一幂等键重放返回原结果，不产生新事件、不重复计数。"""
        self._require(cmd.role, access.COMMANDER, access.GRID)
        self._check_task_scope(cmd, task_id)
        person = self._get(self.proj.people, person_id, "人员")
        if not idem_key:
            raise GeoHazardError("回执必须携带幂等键")
        try:
            evt = self._append(
                cmd,
                "CallReceiptConfirmed",
                {"task_id": task_id, "person_id": person_id, "channel": channel},
                idem_key=idem_key,
            )
        except IdempotentReplay as replay:
            return {
                "idempotent": True,
                "event_seq": replay.receipt.seq,
                "person_id": person_id,
                "warned": person["warned"],
            }
        return {"idempotent": False, "event_seq": evt.seq, "person_id": person_id, "warned": True}

    def record_refusal(self, cmd: Command, task_id: str, person_id: str, reason: str) -> dict:
        """拒绝转移保留事实并触发替代方案（上门劝离/强制组织转移建议）。"""
        self._require(cmd.role, access.COMMANDER, access.GRID)
        self._check_task_scope(cmd, task_id)
        self._get(self.proj.people, person_id, "人员")
        seq = self._append(
            cmd,
            "PersonRefused",
            {"task_id": task_id, "person_id": person_id, "reason": reason},
        ).seq
        alt_id = f"ALT-REFUSE-{task_id}-{person_id}"
        if not self.proj.open_alternatives_for(person_id=person_id, task_id=task_id):
            self._append(
                cmd,
                "AlternativePlanOpened",
                {
                    "alt_id": alt_id,
                    "task_id": task_id,
                    "person_id": person_id,
                    "reason": f"住户拒绝转移: {reason}",
                    "measure": "网格干部与救援队伍上门二次劝离，必要时组织保护性转移",
                },
            )
        return {"event_seq": seq, "alternative": alt_id}

    def report_contact_lost(
        self, cmd: Command, task_id: str, person_id: str, last_channel: str = "",
    ) -> dict:
        """通信中断保留事实并触发替代方案（卫星电话/敲门行动/队伍寻人）。"""
        self._require(cmd.role, access.COMMANDER, access.GRID)
        self._check_task_scope(cmd, task_id)
        person = self._get(self.proj.people, person_id, "人员")
        seq = self._append(
            cmd,
            "ContactLost",
            {"task_id": task_id, "person_id": person_id, "last_channel": last_channel},
        ).seq
        alt_id = f"ALT-LOST-{task_id}-{person_id}"
        if not self.proj.open_alternatives_for(person_id=person_id, task_id=task_id):
            self._append(
                cmd,
                "AlternativePlanOpened",
                {
                    "alt_id": alt_id,
                    "task_id": task_id,
                    "person_id": person_id,
                    "reason": "通信中断，叫应无回执",
                    "measure": "派出队伍实地核查，启用卫星电话与邻户互查",
                },
            )
        return {"event_seq": seq, "alternative": alt_id}

    def _check_task_scope(self, cmd: Command, task_id: str) -> None:
        task = self._get(self.proj.tasks, task_id, "任务")
        # 网格员岗位账号约定为 "grid:<网格号>"，只能办理本网格任务。
        if cmd.role == access.GRID and cmd.actor != f"grid:{task['grid_id']}":
            raise PermissionDeniedError("网格员只能办理本网格任务")

    # ---- 转移路线与安置 ----
    def start_transfer(self, cmd: Command, task_id: str, person_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        if cmd.role == access.GRID:
            self._check_task_scope(cmd, task_id)
        task = self._get(self.proj.tasks, task_id, "任务")
        person = self._get(self.proj.people, person_id, "人员")
        if person["verified"]:
            return {"event_seq": None, "already_safe": True}
        if not task.get("route_id") or self.proj.routes[task["route_id"]]["status"] != "open":
            raise GeoHazardError("转移路线不可用，请先按替代方案开辟路线")
        evt = self._append(
            cmd,
            "TransferStarted",
            {"task_id": task_id, "person_id": person_id, "route_id": task["route_id"]},
        )
        return {"event_seq": evt.seq, "route_id": task["route_id"]}

    def report_arrival(
        self, cmd: Command, person_id: str, shelter_id: str, idem_key: str = "",
    ) -> dict:
        """到达安全地点报到。已到达/已入住的重复报到幂等，绝不重复计数。"""
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        person = self._get(self.proj.people, person_id, "人员")
        if person["status"] in ("arrived", "safe"):
            return {
                "idempotent": True,
                "person_id": person_id,
                "shelter_id": person["arrival_shelter"],
                "counted": False,
            }
        shelter = self._get(self.proj.shelters, shelter_id, "安置点")
        if shelter["occupancy"] + shelter["arrived"] + 1 > shelter["capacity"]:
            raise GeoHazardError(f"安置点 {shelter_id} 容量不足")
        kw = {"idem_key": idem_key} if idem_key else {}
        try:
            evt = self._append(
                cmd,
                "PersonArrived",
                {"person_id": person_id, "shelter_id": shelter_id},
                **kw,
            )
        except IdempotentReplay as replay:
            return {"idempotent": True, "event_seq": replay.receipt.seq, "counted": False}
        return {"idempotent": False, "event_seq": evt.seq, "shelter_id": shelter_id, "counted": True}

    def check_in_shelter(self, cmd: Command, person_id: str, shelter_id: str) -> dict:
        """安置岗位复核入住：同一人只计一次容量。"""
        self._require(cmd.role, access.SHELTER)
        if cmd.actor != f"shelter:{shelter_id}":
            raise PermissionDeniedError("安置岗位只能办理本安置点入住")
        person = self._get(self.proj.people, person_id, "人员")
        shelter = self._get(self.proj.shelters, shelter_id, "安置点")
        if person["verified"]:
            return {"idempotent": True, "occupancy": shelter["occupancy"]}
        if person["status"] != "arrived" or person["arrival_shelter"] != shelter_id:
            raise GeoHazardError("人员尚未到达本安置点，不能办理入住")
        evt = self._append(
            cmd,
            "ShelterCheckedIn",
            {"person_id": person_id, "shelter_id": shelter_id},
        )
        return {"event_seq": evt.seq, "occupancy": self.proj.shelters[shelter_id]["occupancy"]}

    # ---- 替代方案 ----
    def open_alternative(
        self, cmd: Command, alt_id: str, reason: str, measure: str,
        task_id: str = "", person_id: str = "", old_route_id: str = "",
        new_route_id: str = "", team_id: str = "",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        if alt_id in self.proj.alternatives:
            raise GeoHazardError("替代方案编号已存在")
        if new_route_id:
            route = self._get(self.proj.routes, new_route_id, "备用路线")
            if route["status"] != "open":
                raise GeoHazardError("备用路线未开放")
        evt = self._append(
            cmd,
            "AlternativePlanOpened",
            {
                "alt_id": alt_id,
                "task_id": task_id,
                "person_id": person_id,
                "reason": reason,
                "measure": measure,
                "old_route_id": old_route_id,
                "new_route_id": new_route_id,
                "team_id": team_id,
            },
        )
        if new_route_id:
            self._get(self.proj.routes, new_route_id, "备用路线")
        return {"event_seq": evt.seq}

    def close_alternative(self, cmd: Command, alt_id: str, resolution: str) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID, access.RESCUE)
        alt = self._get(self.proj.alternatives, alt_id, "替代方案")
        if alt["status"] != "open":
            raise GeoHazardError("替代方案已闭环")
        evt = self._append(
            cmd,
            "AlternativePlanClosed",
            {"alt_id": alt_id, "resolution": resolution},
        )
        return {"event_seq": evt.seq}

    # ---- 救援力量 ----
    def define_team(self, cmd: Command, team_id: str, name: str, home_region: str = "") -> dict:
        self._require(cmd.role, access.COMMANDER)
        evt = self._append(
            cmd,
            "TeamDefined",
            {"team_id": team_id, "name": name, "home_region": home_region},
        )
        return {"event_seq": evt.seq}

    def preposition_team(self, cmd: Command, team_id: str, zone_id: str, task_id: str = "") -> dict:
        """力量前置：风险升级时把队伍部署到受影响点区。"""
        self._require(cmd.role, access.COMMANDER)
        self._get(self.proj.teams, team_id, "救援队伍")
        self._get(self.proj.zones, zone_id, "风险点区")
        team = self.proj.teams[team_id]
        if team["status"] == "dispatched":
            raise GeoHazardError("队伍已在任务中，不能重复前置")
        evt = self._append(
            cmd,
            "TeamPrepositioned",
            {"team_id": team_id, "zone_id": zone_id, "task_id": task_id},
        )
        return {"event_seq": evt.seq}

    def dispatch_team(
        self, cmd: Command, team_id: str, task_id: str = "", alt_id: str = "", to_zone: str = "",
    ) -> dict:
        self._require(cmd.role, access.COMMANDER)
        team = self._get(self.proj.teams, team_id, "救援队伍")
        if team["status"] == "dispatched":
            raise GeoHazardError("队伍已在任务中，不能重复派遣")
        if not task_id and not alt_id:
            raise GeoHazardError("派遣必须指定任务或替代方案")
        evt = self._append(
            cmd,
            "TeamDispatched",
            {"team_id": team_id, "task_id": task_id, "alt_id": alt_id, "to_zone": to_zone},
        )
        return {"event_seq": evt.seq}

    def return_team(self, cmd: Command, team_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER)
        evt = self._append(cmd, "TeamReturned", {"team_id": team_id})
        return {"event_seq": evt.seq}

    # ---- 跨区支援（队伍 + 物资不双重占用） ----
    def offer_support(
        self, cmd: Command, support_id: str, team_id: str, from_region: str, to_region: str,
        material_batch_id: str = "", material_qty: int = 0,
    ) -> dict:
        """外区支援意向：携带物资在源端仓库立即预留冻结，受援端不可再领。"""
        self._require(cmd.role, access.COMMANDER)
        if support_id in self.proj.supports:
            raise GeoHazardError("支援单已存在")
        self._get(self.proj.teams, team_id, "救援队伍")
        reservation_id = ""
        if material_batch_id:
            if material_qty <= 0:
                raise GeoHazardError("支援物资数量必须为正")
            self._assert_stock_free("WAREHOUSE", material_batch_id, material_qty, frozen=True)
            reservation_id = f"RSV-SUP-{support_id}"
            self._append(
                cmd,
                "MaterialReserved",
                {
                    "reservation_id": reservation_id,
                    "location": "WAREHOUSE",
                    "batch_id": material_batch_id,
                    "qty": material_qty,
                    "purpose": f"跨区支援 {from_region}->{to_region}",
                },
            )
        evt = self._append(
            cmd,
            "CrossRegionSupportOffered",
            {
                "support_id": support_id,
                "team_id": team_id,
                "from_region": from_region,
                "to_region": to_region,
                "material_batch_id": material_batch_id,
                "material_qty": material_qty,
                "reservation_id": reservation_id,
            },
        )
        return {"event_seq": evt.seq, "reservation_id": reservation_id}

    def accept_support(self, cmd: Command, support_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER)
        support = self._get(self.proj.supports, support_id, "跨区支援单")
        if support["status"] != "offered":
            raise GeoHazardError("支援单不在待接收状态")
        payload = {"support_id": support_id}
        if support["reservation_id"]:
            payload["reservation_id"] = support["reservation_id"]
        evt = self._append(cmd, "CrossRegionSupportAccepted", payload)
        if support["material_batch_id"]:
            # 物资正式出库启运：源端库存与预留同时核销，在途只挂支援单
            self._append(
                cmd,
                "MaterialIssued",
                {"reservation_id": support["reservation_id"]},
            )
        return {"event_seq": evt.seq}

    def release_support(self, cmd: Command, support_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER)
        support = self._get(self.proj.supports, support_id, "跨区支援单")
        if support["status"] != "offered":
            raise GeoHazardError("仅待接收的支援可撤回")
        evt = self._append(cmd, "CrossRegionSupportReleased", {"support_id": support_id})
        if support["reservation_id"]:
            self._append(
                cmd,
                "ReservationReleased",
                {"reservation_id": support["reservation_id"]},
            )
        return {"event_seq": evt.seq}

    # ---- 物资批次、前置、预留、领用、交接 ----
    def receive_batch(
        self, cmd: Command, batch_id: str, material: str, qty: int, unit: str = "",
    ) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        if batch_id in self.proj.batches:
            raise GeoHazardError("批次号已存在")
        if qty <= 0:
            raise GeoHazardError("入库数量必须为正")
        evt = self._append(
            cmd,
            "MaterialBatchReceived",
            {"batch_id": batch_id, "material": material, "qty": qty, "unit": unit},
        )
        return {"event_seq": evt.seq}

    def preposition_stock(
        self, cmd: Command, batch_id: str, qty: int, to_location: str,
        from_location: str = "WAREHOUSE",
    ) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        self._get(self.proj.batches, batch_id, "物资批次")
        self._assert_not_in_handover(from_location)
        self._assert_not_in_handover(to_location)
        if self.proj.stock_on_hand(from_location, batch_id) < qty:
            raise GeoHazardError("前置数量超过现有库存")
        evt = self._append(
            cmd,
            "StockPrepositioned",
            {
                "batch_id": batch_id,
                "qty": qty,
                "from_location": from_location,
                "to_location": to_location,
            },
        )
        return {"event_seq": evt.seq}

    def reserve_material(
        self, cmd: Command, reservation_id: str, location: str, batch_id: str,
        qty: int, purpose: str = "",
    ) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER, access.RESCUE)
        if reservation_id in self.proj.reservations:
            raise GeoHazardError("预留单号已存在")
        self._assert_stock_free(location, batch_id, qty, frozen=True)
        evt = self._append(
            cmd,
            "MaterialReserved",
            {
                "reservation_id": reservation_id,
                "location": location,
                "batch_id": batch_id,
                "qty": qty,
                "purpose": purpose,
            },
            idem_key=reservation_id,
        )
        return {"event_seq": evt.seq}

    def issue_material(self, cmd: Command, reservation_id: str) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        resv = self._get(self.proj.reservations, reservation_id, "预留单")
        if resv["status"] != "reserved":
            raise GeoHazardError("预留单不可领用（已发放或已释放）")
        self._assert_not_in_handover(resv["location"])
        evt = self._append(cmd, "MaterialIssued", {"reservation_id": reservation_id})
        return {"event_seq": evt.seq}

    def release_reservation(self, cmd: Command, reservation_id: str) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        resv = self._get(self.proj.reservations, reservation_id, "预留单")
        if resv["status"] != "reserved":
            raise GeoHazardError("预留单不在有效状态")
        evt = self._append(cmd, "ReservationReleased", {"reservation_id": reservation_id})
        return {"event_seq": evt.seq}

    def _assert_stock_free(self, location: str, batch_id: str, qty: int, frozen: bool) -> None:
        self._assert_not_in_handover(location)
        available = self.proj.stock_available(location, batch_id)
        if available < qty:
            raise GeoHazardError(
                f"物资 {batch_id} 在 {location} 可占用量不足: 需要{qty}, 可用{available}"
            )

    def _assert_not_in_handover(self, location: str) -> None:
        for handover in self.proj.handovers.values():
            if handover["location"] == location and handover["status"] == "pending":
                raise GeoHazardError(f"{location} 库存交接待接收，暂时冻结")

    def handover_stock(
        self, cmd: Command, handover_id: str, location: str, from_party: str, to_party: str,
    ) -> dict:
        """库存交接：按当前库存生成账实快照，接收完成前冻结该点位。系统恢复
        后未接收的交接仍在待办清单中，可继续办理。"""
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        if handover_id in self.proj.handovers:
            raise GeoHazardError("交接单已存在")
        snapshot = {
            batch: qty for batch, qty in self.proj.stock.get(location, {}).items() if qty
        }
        evt = self._append(
            cmd,
            "StockHandover",
            {
                "handover_id": handover_id,
                "location": location,
                "from_party": from_party,
                "to_party": to_party,
                "snapshot": snapshot,
            },
        )
        return {"event_seq": evt.seq, "snapshot": snapshot}

    def accept_handover(self, cmd: Command, handover_id: str) -> dict:
        self._require(cmd.role, access.LOGISTICS, access.COMMANDER)
        handover = self._get(self.proj.handovers, handover_id, "交接单")
        if handover["status"] != "pending":
            raise GeoHazardError("交接单已接收")
        for batch, qty in handover["snapshot"].items():
            if self.proj.stock_on_hand(handover["location"], batch) != qty:
                raise GeoHazardError("账实不符，不能接收交接")
        evt = self._append(cmd, "StockHandoverAccepted", {"handover_id": handover_id})
        return {"event_seq": evt.seq}

    # ---- 双岗确认任务完成 ----
    def sign_task_completed(self, cmd: Command, task_id: str) -> dict:
        self._require(cmd.role, access.COMMANDER, access.GRID)
        task = self._get(self.proj.tasks, task_id, "任务")
        if cmd.role == access.GRID and cmd.actor != f"grid:{task['grid_id']}":
            raise PermissionDeniedError("网格员只能会签本网格任务")
        if task["status"] == "completed":
            raise GeoHazardError("任务已闭环")
        if cmd.actor in task["signers"]:
            raise GeoHazardError("同一人不得重复签字")
        progress = self.proj.task_progress(task)
        if progress["unaccounted"]:
            raise GeoHazardError(
                f"仍有 {progress['unaccounted']} 人未确认安全且无在办替代方案，不能闭环"
            )
        if self.proj.open_alternatives_for(task_id=task_id):
            raise GeoHazardError("该任务仍有在办替代方案，须先闭环再确认任务完成")
        # 第一签字人若是发令人本人，允许其签字留痕，但必须再有一名非发令人会签；
        # 非发令人签后任务即达成"双岗且含独立确认"。
        signers_after = list(task["signers"]) + [cmd.actor]
        issuer = task["issued_by"]
        independent = [s for s in signers_after if s != issuer]
        if len(signers_after) >= 2 and independent:
            evt = self._append(cmd, "TaskConfirmSigned", {"task_id": task_id})
            return {"event_seq": evt.seq, "status": "completed", "progress": progress}
        evt = self._append(cmd, "TaskConfirmSigned", {"task_id": task_id})
        return {
            "event_seq": evt.seq,
            "status": "open",
            "note": "发令人不能独自确认，还需一名非发令人会签",
            "progress": progress,
        }

    # ---- 查询 ----
    def task_status(self, task_id: str) -> dict:
        task = self._get(self.proj.tasks, task_id, "任务")
        return {
            "task_id": task_id,
            "status": task["status"],
            "kinds": task["kinds"],
            "level": task["level"],
            "signers": sorted(task["signers"]),
            "progress": self.proj.task_progress(task),
            "open_alternatives": [
                a["alt_id"]
                for a in self.proj.open_alternatives_for(task_id=task_id)
            ],
        }

    def persons_for(self, role: str, scope: str | None = None) -> list[dict]:
        """按岗位返回人员视图：敏感信息只对本网格/本安置点岗位开放。"""
        return access.view_persons(role, scope, list(self.proj.people.values()))

    def dashboard(self) -> dict:
        return self.proj.dashboard()
