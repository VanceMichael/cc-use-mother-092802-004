"""只读投影：从只追加事件重放当前态势。

投影可随时整体丢弃重建，因此系统重启后状态不丢；把重放截止到某次
ResponseClosed 之前，即可还原解除当时的风险、人员、路线、力量与物资。

人员安全计数只认每人唯一的当前状态：叫应回执、到达、入住都按人去重，
任何重复事件都不会让同一个人被计算两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .events import Event
from .timeutil import add_minutes, earlier

RISK_ORDER = {"blue": 1, "yellow": 2, "orange": 3, "red": 4}
RISK_CN = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色"}

KIND_REGISTERED = "registered_point"
KIND_GULLY = "gully_mouth"
KIND_CLIFF = "cliff_slope"
KIND_SITE = "construction_site"
KIND_SCENIC = "scenic_area"

CALL_SLA_MINUTES = 30


@dataclass
class Projection:
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_order: list[str] = field(default_factory=list)
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    assessments: dict[str, dict[str, Any]] = field(default_factory=dict)
    assessment_order: list[str] = field(default_factory=list)
    grids: dict[str, dict[str, Any]] = field(default_factory=dict)
    zones: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    shelters: dict[str, dict[str, Any]] = field(default_factory=dict)
    teams: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    alternatives: dict[str, dict[str, Any]] = field(default_factory=dict)
    supports: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: dict[str, dict[str, Any]] = field(default_factory=dict)
    stock: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    reserved: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    handovers: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    closures: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_ts: str = ""

    # ---- 重放 ----
    @classmethod
    def rebuild(cls, events: Iterable[Event]) -> "Projection":
        proj = cls()
        for evt in events:
            proj.apply(evt)
        return proj

    def apply(self, evt: Event) -> None:
        p = evt.payload
        self.last_ts = evt.ts
        handler = getattr(self, f"_on_{evt.etype}", None)
        if handler is None:
            raise ValueError(f"未知事件类型: {evt.etype}")
        handler(evt, p)

    # ---- 响应 ----
    def _on_ResponseOpened(self, e: Event, p: dict) -> None:
        self.responses[p["response_id"]] = {
            "response_id": p["response_id"],
            "level": p["level"],
            "region": p["region"],
            "opened_ts": e.ts,
            "opened_by": e.actor,
            "basis": p.get("basis", e.basis),
            "status": "active",
            "open_seq": e.seq,
        }
        self.response_order.append(p["response_id"])

    def _on_ResponseClosureApproved(self, e: Event, p: dict) -> None:
        self.approvals.setdefault(p["response_id"], []).append(
            {"approver": e.actor, "role": e.role, "ts": e.ts, "note": p.get("note", "")}
        )

    def _on_ResponseClosed(self, e: Event, p: dict) -> None:
        resp = self.responses[p["response_id"]]
        resp["status"] = "closed"
        resp["closed_ts"] = e.ts
        resp["close_seq"] = e.seq
        self.closures[p["response_id"]] = {
            "response_id": p["response_id"],
            "closed_ts": e.ts,
            "closed_by": e.actor,
            "open_seq": resp["open_seq"],
            "close_seq": e.seq,
            "snapshot": p.get("snapshot", {}),
        }

    # ---- 雨情与研判 ----
    def _on_RainReportRecorded(self, e: Event, p: dict) -> None:
        self.reports[p["report_id"]] = {
            "report_id": p["report_id"],
            "station": p["station"],
            "issued_at": p["issued_at"],
            "recorded_ts": e.ts,
            "late": earlier(p["issued_at"], e.ts) or p.get("late", False),
            "cumulative_mm": p["cumulative_mm"],
            "intensity_mm_h": p.get("intensity_mm_h", 0),
        }

    def _on_RiskAssessmentIssued(self, e: Event, p: dict) -> None:
        self.assessments[p["assessment_id"]] = {
            "assessment_id": p["assessment_id"],
            "level": p["level"],
            "zone_ids": list(p["zone_ids"]),
            "basis_reports": list(p.get("basis_reports", [])),
            "note": p.get("note", ""),
            "issued_ts": e.ts,
            "issued_by": e.actor,
            "seq": e.seq,
        }
        self.assessment_order.append(p["assessment_id"])

    # ---- 点区 / 网格 / 路线 / 安置点 ----
    def _on_GridDefined(self, e: Event, p: dict) -> None:
        self.grids[p["grid_id"]] = {"grid_id": p["grid_id"], "name": p["name"], "leader": e.actor}

    def _on_ZoneRegistered(self, e: Event, p: dict) -> None:
        self.zones[p["zone_id"]] = {
            "zone_id": p["zone_id"],
            "name": p["name"],
            "kind": p["kind"],
            "grid_id": p["grid_id"],
            "risk_level": p.get("risk_level", "blue"),
        }

    def _on_RouteDefined(self, e: Event, p: dict) -> None:
        self.routes[p["route_id"]] = {
            "route_id": p["route_id"],
            "name": p.get("name", p["route_id"]),
            "zone_ids": list(p.get("zone_ids", [])),
            "shelter_id": p["shelter_id"],
            "status": "open",
            "history": [{"ts": e.ts, "status": "open"}],
        }

    def _on_RouteClosed(self, e: Event, p: dict) -> None:
        route = self.routes[p["route_id"]]
        route["status"] = "closed"
        route["history"].append({"ts": e.ts, "status": "closed", "reason": p["reason"]})

    def _on_RouteReopened(self, e: Event, p: dict) -> None:
        route = self.routes[p["route_id"]]
        route["status"] = "open"
        route["history"].append({"ts": e.ts, "status": "open"})

    def _on_ShelterDefined(self, e: Event, p: dict) -> None:
        self.shelters[p["shelter_id"]] = {
            "shelter_id": p["shelter_id"],
            "name": p["name"],
            "capacity": p["capacity"],
            "occupancy": 0,
            "arrived": 0,
        }

    # ---- 队伍 ----
    def _on_TeamDefined(self, e: Event, p: dict) -> None:
        self.teams[p["team_id"]] = {
            "team_id": p["team_id"],
            "name": p["name"],
            "home_region": p.get("home_region", ""),
            "status": "standby",
            "position": "",
            "assignment": "",
        }

    def _on_TeamPrepositioned(self, e: Event, p: dict) -> None:
        team = self.teams[p["team_id"]]
        team["status"] = "prepositioned"
        team["position"] = p["zone_id"]
        team["assignment"] = p.get("task_id", "")

    def _on_TeamDispatched(self, e: Event, p: dict) -> None:
        team = self.teams[p["team_id"]]
        team["status"] = "dispatched"
        team["assignment"] = p.get("task_id") or p.get("alt_id", "")
        team["position"] = p.get("to_zone", team["position"])

    def _on_TeamReturned(self, e: Event, p: dict) -> None:
        team = self.teams[p["team_id"]]
        team["status"] = "standby"
        team["assignment"] = ""
        team["position"] = ""

    # ---- 分组与人员 ----
    def _on_GroupRegistered(self, e: Event, p: dict) -> None:
        self.groups[p["group_id"]] = {
            "group_id": p["group_id"],
            "kind": p.get("kind", "resident"),
            "zone_id": p.get("zone_id", ""),
            "grid_id": p.get("grid_id", ""),
            "name": p.get("name", p["group_id"]),
        }

    def _on_PersonRegistered(self, e: Event, p: dict) -> None:
        self.people[p["person_id"]] = {
            "person_id": p["person_id"],
            "name": p["name"],
            "kind": p.get("kind", "resident"),
            "group_id": p.get("group_id", ""),
            "grid_id": p.get("grid_id", ""),
            "zone_id": p.get("zone_id", ""),
            "id_number": p.get("id_number", ""),
            "phone": p.get("phone", ""),
            "care_need": p.get("care_need", ""),
            "status": "registered",
            "warned": False,
            "receipt_ts": "",
            "arrival_shelter": "",
            "arrival_ts": "",
            "verified": False,
            "verified_ts": "",
            "refused": False,
            "refused_active": False,
            "contact_lost": False,
            "route_id": "",
            "facts": [],
        }

    def _on_CallAttempted(self, e: Event, p: dict) -> None:
        key = (p["task_id"], p["person_id"])
        slot = self.calls.setdefault(key, {"attempts": [], "receipt": None})
        slot["attempts"].append({"ts": e.ts, "channel": p["channel"], "result": p["result"]})

    def _on_CallReceiptConfirmed(self, e: Event, p: dict) -> None:
        key = (p["task_id"], p["person_id"])
        slot = self.calls.setdefault(key, {"attempts": [], "receipt": None})
        slot["receipt"] = {"ts": e.ts, "channel": p.get("channel", ""), "idem_key": e.idem_key}
        person = self.people[p["person_id"]]
        person["warned"] = True
        person["receipt_ts"] = e.ts
        person["status"] = "warned" if person["status"] == "registered" else person["status"]

    def _on_PersonRefused(self, e: Event, p: dict) -> None:
        person = self.people[p["person_id"]]
        person["refused"] = True
        person["refused_active"] = True
        person["facts"].append({"kind": "refusal", "ts": e.ts, "reason": p["reason"]})

    def _on_ContactLost(self, e: Event, p: dict) -> None:
        person = self.people[p["person_id"]]
        person["contact_lost"] = True
        person["facts"].append(
            {"kind": "contact_lost", "ts": e.ts, "last_channel": p.get("last_channel", "")}
        )

    def _on_TransferStarted(self, e: Event, p: dict) -> None:
        person = self.people[p["person_id"]]
        person["status"] = "transferring"
        person["route_id"] = p.get("route_id", "")
        person["refused_active"] = False

    def _on_PersonArrived(self, e: Event, p: dict) -> None:
        person = self.people[p["person_id"]]
        shelter = self.shelters[p["shelter_id"]]
        person["status"] = "arrived"
        person["arrival_shelter"] = p["shelter_id"]
        person["arrival_ts"] = e.ts
        person["contact_lost"] = False
        shelter["arrived"] += 1

    def _on_ShelterCheckedIn(self, e: Event, p: dict) -> None:
        person = self.people[p["person_id"]]
        shelter = self.shelters[p["shelter_id"]]
        person["verified"] = True
        person["verified_ts"] = e.ts
        person["status"] = "safe"
        shelter["arrived"] -= 1
        shelter["occupancy"] += 1

    # ---- 任务与替代方案 ----
    def _on_TaskGenerated(self, e: Event, p: dict) -> None:
        self.tasks[p["task_id"]] = {
            "task_id": p["task_id"],
            "assessment_id": p["assessment_id"],
            "zone_id": p["zone_id"],
            "grid_id": p["grid_id"],
            "kinds": list(p["kinds"]),
            "group_ids": list(p["group_ids"]),
            "route_id": p.get("route_id", ""),
            "shelter_id": p.get("shelter_id", ""),
            "level": p["level"],
            "issued_by": e.actor,
            "created_ts": e.ts,
            "deadline_ts": add_minutes(e.ts, p.get("sla_minutes", CALL_SLA_MINUTES)),
            "signers": {},
            "status": "open",
        }

    def _on_TaskEscalated(self, e: Event, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        for kind in p["add_kinds"]:
            if kind not in task["kinds"]:
                task["kinds"].append(kind)
        task["level"] = p["level"]

    def _on_TaskRouteRerouted(self, e: Event, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        task["route_id"] = p["new_route_id"]
        task["shelter_id"] = self.routes[p["new_route_id"]]["shelter_id"]
        task.setdefault("route_history", []).append(
            {"ts": e.ts, "old_route_id": p["old_route_id"], "new_route_id": p["new_route_id"]}
        )

    def _on_TaskConfirmSigned(self, e: Event, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        task["signers"][e.actor] = {"role": e.role, "ts": e.ts}
        if len(task["signers"]) >= 2 and any(
            signer != task["issued_by"] for signer in task["signers"]
        ):
            task["status"] = "completed"
            task["completed_ts"] = e.ts

    def _on_AlternativePlanOpened(self, e: Event, p: dict) -> None:
        self.alternatives[p["alt_id"]] = {
            "alt_id": p["alt_id"],
            "task_id": p.get("task_id", ""),
            "person_id": p.get("person_id", ""),
            "reason": p["reason"],
            "measure": p["measure"],
            "old_route_id": p.get("old_route_id", ""),
            "new_route_id": p.get("new_route_id", ""),
            "team_id": p.get("team_id", ""),
            "opened_ts": e.ts,
            "status": "open",
        }

    def _on_AlternativePlanClosed(self, e: Event, p: dict) -> None:
        self.alternatives[p["alt_id"]]["status"] = "closed"
        self.alternatives[p["alt_id"]]["resolution"] = p.get("resolution", "")
        self.alternatives[p["alt_id"]]["closed_ts"] = e.ts

    # ---- 跨区支援 ----
    def _on_CrossRegionSupportOffered(self, e: Event, p: dict) -> None:
        self.supports[p["support_id"]] = {
            "support_id": p["support_id"],
            "team_id": p["team_id"],
            "from_region": p["from_region"],
            "to_region": p["to_region"],
            "material_batch_id": p.get("material_batch_id", ""),
            "material_qty": p.get("material_qty", 0),
            "reservation_id": p.get("reservation_id", ""),
            "status": "offered",
        }

    def _on_CrossRegionSupportAccepted(self, e: Event, p: dict) -> None:
        support = self.supports[p["support_id"]]
        support["status"] = "accepted"
        support["reservation_id"] = p.get("reservation_id", "")
        team = self.teams[support["team_id"]]
        team["status"] = "dispatched"
        team["assignment"] = f"support:{support['support_id']}"

    def _on_CrossRegionSupportReleased(self, e: Event, p: dict) -> None:
        support = self.supports[p["support_id"]]
        support["status"] = "released"
        team = self.teams[support["team_id"]]
        if team["assignment"] == f"support:{support['support_id']}":
            team["status"] = "standby"
            team["assignment"] = ""

    # ---- 物资 ----
    def _on_MaterialBatchReceived(self, e: Event, p: dict) -> None:
        self.batches[p["batch_id"]] = {
            "batch_id": p["batch_id"],
            "material": p["material"],
            "unit": p.get("unit", ""),
            "qty": p["qty"],
        }
        self.stock.setdefault("WAREHOUSE", {}).setdefault(p["batch_id"], 0)
        self.stock["WAREHOUSE"][p["batch_id"]] += p["qty"]

    def _on_StockPrepositioned(self, e: Event, p: dict) -> None:
        qty = p["qty"]
        src = self._loc_batch(self.stock, p["from_location"], p["batch_id"])
        if src < qty:
            raise ValueError("重放失败：前置数量超过库存")
        self.stock[p["from_location"]][p["batch_id"]] = src - qty
        self.stock.setdefault(p["to_location"], {}).setdefault(p["batch_id"], 0)
        self.stock[p["to_location"]][p["batch_id"]] += qty

    def _on_MaterialReserved(self, e: Event, p: dict) -> None:
        self.reservations[p["reservation_id"]] = {
            "reservation_id": p["reservation_id"],
            "location": p["location"],
            "batch_id": p["batch_id"],
            "qty": p["qty"],
            "purpose": p.get("purpose", ""),
            "status": "reserved",
        }
        reserved = self._loc_batch(self.reserved, p["location"], p["batch_id"])
        self.reserved[p["location"]][p["batch_id"]] = reserved + p["qty"]

    def _on_MaterialIssued(self, e: Event, p: dict) -> None:
        resv = self.reservations[p["reservation_id"]]
        resv["status"] = "issued"
        loc, batch, qty = resv["location"], resv["batch_id"], resv["qty"]
        self.stock[loc][batch] -= qty
        self.reserved[loc][batch] -= qty

    def _on_ReservationReleased(self, e: Event, p: dict) -> None:
        resv = self.reservations[p["reservation_id"]]
        resv["status"] = "released"
        loc, batch, qty = resv["location"], resv["batch_id"], resv["qty"]
        self.reserved[loc][batch] -= qty

    def _on_StockHandover(self, e: Event, p: dict) -> None:
        self.handovers[p["handover_id"]] = {
            "handover_id": p["handover_id"],
            "location": p["location"],
            "from_party": p["from_party"],
            "to_party": p["to_party"],
            "snapshot": dict(p["snapshot"]),
            "status": "pending",
            "ts": e.ts,
        }

    def _on_StockHandoverAccepted(self, e: Event, p: dict) -> None:
        handover = self.handovers[p["handover_id"]]
        handover["status"] = "accepted"
        handover["accepted_ts"] = e.ts
        handover["accepted_by"] = e.actor

    # ---- 查询辅助 ----
    @staticmethod
    def _loc_batch(table: dict, location: str, batch_id: str) -> int:
        return table.setdefault(location, {}).get(batch_id, 0)

    def stock_on_hand(self, location: str, batch_id: str) -> int:
        return self._loc_batch(self.stock, location, batch_id)

    def stock_available(self, location: str, batch_id: str) -> int:
        """可占用量：现有库存减已预留；交接冻结的点位由服务层另行拦截。"""
        return self.stock_on_hand(location, batch_id) - self._loc_batch(
            self.reserved, location, batch_id
        )

    def active_response(self) -> dict[str, Any] | None:
        for rid in reversed(self.response_order):
            if self.responses[rid]["status"] == "active":
                return self.responses[rid]
        return None

    def latest_assessment(self) -> dict[str, Any] | None:
        for aid in reversed(self.assessment_order):
            return self.assessments[aid]
        return None

    def task_people(self, task: dict) -> list[dict]:
        ids = [
            pid
            for pid, person in self.people.items()
            if person["group_id"] in task["group_ids"]
        ]
        return [self.people[pid] for pid in sorted(ids)]

    def open_alternatives_for(self, *, person_id: str = "", task_id: str = "") -> list[dict]:
        return [
            alt
            for alt in self.alternatives.values()
            if alt["status"] == "open"
            and (not person_id or alt["person_id"] == person_id)
            and (not task_id or alt["task_id"] == task_id)
        ]

    def is_accounted(self, person: dict, task: dict) -> bool:
        """任务口径下人员是否已有着落：安全入住，或拒绝/失联已有在办替代方案。"""
        if person["verified"] and person["arrival_shelter"]:
            return True
        if (person["refused_active"] or person["contact_lost"]) and self.open_alternatives_for(
            person_id=person["person_id"], task_id=task["task_id"]
        ):
            return True
        return False

    def task_progress(self, task: dict) -> dict[str, int]:
        people = self.task_people(task)
        warned = sum(1 for x in people if x["warned"])
        safe = sum(1 for x in people if x["verified"])
        accounted = sum(1 for x in people if self.is_accounted(x, task))
        return {
            "total": len(people),
            "warned": warned,
            "safe": safe,
            "accounted": accounted,
            "unaccounted": len(people) - accounted,
        }

    def overdue_calls(self, as_of: str) -> list[dict]:
        """逾期叫应：任务已过办理时限，仍有人未给叫应回执。"""
        result = []
        for task in self.tasks.values():
            if task["status"] != "open":
                continue
            if earlier(as_of, task["deadline_ts"]):
                continue
            for person in self.task_people(task):
                slot = self.calls.get((task["task_id"], person["person_id"]))
                if not slot or not slot["receipt"]:
                    result.append(
                        {
                            "task_id": task["task_id"],
                            "person_id": person["person_id"],
                            "grid_id": person["grid_id"],
                            "deadline_ts": task["deadline_ts"],
                            "last_attempt": (
                                slot["attempts"][-1] if slot and slot["attempts"] else None
                            ),
                        }
                    )
        return result

    def pending_transfer_verifications(self) -> list[dict]:
        """已到达但安置点尚未复核入住的转移人员。"""
        return [
            {
                "person_id": pid,
                "arrival_ts": p["arrival_ts"],
                "shelter_id": p["arrival_shelter"],
                "grid_id": p["grid_id"],
            }
            for pid, p in sorted(self.people.items())
            if p["status"] == "arrived" and not p["verified"]
        ]

    def pending_handovers(self) -> list[dict]:
        return [h for h in self.handovers.values() if h["status"] == "pending"]

    def resume(self, as_of: str) -> dict[str, list]:
        """系统恢复后的续办清单。"""
        return {
            "overdue_calls": self.overdue_calls(as_of),
            "pending_verifications": self.pending_transfer_verifications(),
            "pending_handovers": self.pending_handovers(),
        }

    def safe_person_ids(self) -> set[str]:
        return {pid for pid, p in self.people.items() if p["verified"]}

    def dashboard(self) -> dict[str, Any]:
        zones_risk = {}
        for aid in self.assessment_order:
            assess = self.assessments[aid]
            for zid in assess["zone_ids"]:
                zones_risk[zid] = assess["level"]
        people = list(self.people.values())
        return {
            "response": (
                {"id": r["response_id"], "status": r["status"], "level": r["level"]}
                if (r := self.active_response())
                else None
            ),
            "zones": len(self.zones),
            "people_total": len(people),
            "warned": sum(1 for p in people if p["warned"]),
            "safe": len(self.safe_person_ids()),
            "transferring": sum(1 for p in people if p["status"] == "transferring"),
            "arrived_pending": sum(1 for p in people if p["status"] == "arrived"),
            "refused_active": sum(1 for p in people if p["refused_active"]),
            "contact_lost": sum(1 for p in people if p["contact_lost"]),
            "tasks_open": sum(1 for t in self.tasks.values() if t["status"] == "open"),
            "tasks_completed": sum(1 for t in self.tasks.values() if t["status"] == "completed"),
            "alternatives_open": sum(
                1 for a in self.alternatives.values() if a["status"] == "open"
            ),
            "zone_risks": zones_risk,
            "shelters": {
                sid: {
                    "capacity": s["capacity"],
                    "occupancy": s["occupancy"],
                    "arrived_pending": s["arrived"],
                }
                for sid, s in self.shelters.items()
            },
            "teams": {tid: {"status": t["status"], "assignment": t["assignment"]} for tid, t in self.teams.items()},
        }
