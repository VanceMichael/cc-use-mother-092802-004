"""地质灾害响应服务的业务约束测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.access import Principal, Role
from src.response_errors import (
    CapacityError,
    ConflictError,
    DoubleAllocationError,
    InsufficientStockError,
    InvalidStateError,
    NotFoundError,
    SeparationOfDutiesError,
)
from src.response_models import (
    GroupKind,
    HoldStatus,
    PersonStatus,
    RouteStatus,
    TaskKind,
    TaskStatus,
    ZoneKind,
)
from src.response_service import ResponseService


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs: float) -> None:
        self.moment += timedelta(**kwargs)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store_path = Path(self.tmp.name) / "events.jsonl"
        self.commander = Principal("cmdr-1", Role.COMMANDER)
        self.gridder1 = Principal("gridder-1", Role.GRID_MEMBER, grid_id="grid-1")
        self.gridder2 = Principal("gridder-2", Role.GRID_MEMBER, grid_id="grid-2")
        self.staff1 = Principal("staff-1", Role.SHELTER_STAFF, shelter_id="shelter-1")
        self.logistics = Principal("log-1", Role.LOGISTICS)
        self.svc = self._build()

    def _build(self) -> ResponseService:
        svc = ResponseService.open(self.store_path, self.clock)
        svc.activate_response("resp-2026-10", 4, ["重庆", "陕西"], self.commander)
        svc.register_grid("grid-1", "沟口网格", ["gridder-1"])
        svc.register_grid("grid-2", "景区网格", ["gridder-2"])
        svc.register_shelter("shelter-1", "中心小学安置点", 100)
        svc.register_shelter("shelter-2", "体育馆安置点", 50)
        svc.register_zone("zone-1", ZoneKind.HAZARD_POINT, "滑坡隐患点", "grid-1")
        svc.register_zone("zone-2", ZoneKind.GULLY_MOUTH, "山区沟口", "grid-1")
        svc.register_zone("zone-3", ZoneKind.SCENIC_AREA, "峡谷景区", "grid-2")
        svc.register_route("route-1", "zone-1", "shelter-1", primary=True)
        svc.register_route("route-2", "zone-2", "shelter-1", primary=True)
        svc.register_route("route-2b", "zone-2", "shelter-2")
        svc.register_route("route-3", "zone-3", "shelter-2", primary=True)
        svc.register_group("group-1", GroupKind.HOUSEHOLD, "grid-1", "zone-1")
        svc.register_group("group-2", GroupKind.HOUSEHOLD, "grid-1", "zone-2")
        svc.register_group("group-3", GroupKind.TOURIST, "grid-2", "zone-3")
        svc.register_person("p-1", "group-1", "张三", "13800000001")
        svc.register_person("p-2", "group-1", "李四", "13800000002", vulnerable=True)
        svc.register_person("p-3", "group-2", "王五", "13800000003")
        svc.register_person("p-4", "group-2", "赵六", "13800000004")
        svc.register_person("p-5", "group-3", "游客甲", "13800000005")
        svc.register_person("p-6", "group-3", "游客乙", "13800000006")
        return svc

    def reopen(self) -> ResponseService:
        self.svc = ResponseService.open(self.store_path, self.clock)
        return self.svc

    def escalate(self, assessment_id: str = "assess-1", level: int = 3, scope: list[str] | None = None):
        self.svc.record_rainfall(
            f"rain-{assessment_id}", "station-1", self.clock.moment, 80.0, self.gridder1
        )
        return self.svc.assess_risk(
            assessment_id,
            self.commander,
            level,
            scope or ["zone-1"],
            {"rainfall_ids": [f"rain-{assessment_id}"], "summary": "持续强降雨"},
        )


class EscalationTest(ServiceCase):
    def test_escalation_generates_verify_and_transfer_tasks(self) -> None:
        result = self.escalate(scope=["zone-1", "zone-2"])
        self.assertTrue(result["escalation"])
        self.assertEqual(
            sorted(result["verify_task_ids"]),
            ["verify:assess-1:zone-1", "verify:assess-1:zone-2"],
        )
        self.assertEqual(result["order_id"], "order:assess-1")
        task1 = self.svc.tasks["transfer:order:assess-1:group-1"]
        task2 = self.svc.tasks["transfer:order:assess-1:group-2"]
        self.assertEqual((task1.route_id, task1.shelter_id), ("route-1", "shelter-1"))
        self.assertEqual((task2.route_id, task2.shelter_id), ("route-2", "shelter-1"))
        self.assertEqual(task1.status, TaskStatus.PENDING)
        self.assertFalse(any(t.group_id == "group-3" for t in self.svc.tasks.values()))
        self.assertEqual(self.svc.risk_level, 3)

    def test_non_escalation_generates_nothing(self) -> None:
        self.escalate("assess-1", 3)
        result = self.escalate("assess-2", 2)
        self.assertFalse(result["escalation"])
        self.assertEqual(result["verify_task_ids"], [])
        self.assertIsNone(result["order_id"])
        self.assertEqual(self.svc.risk_level, 2)

    def test_missing_plan_yields_needs_review_then_resolved(self) -> None:
        self.svc.register_zone("zone-4", ZoneKind.CLIFF_SLOPE, "临崖陡坡", "grid-2")
        self.svc.register_group("group-4", GroupKind.HOUSEHOLD, "grid-2", "zone-4")
        self.svc.register_person("p-7", "group-4", "孙七", "13800000007")
        result = self.escalate(scope=["zone-4"])
        task = self.svc.tasks[result["transfer_task_ids"][0]]
        self.assertEqual(task.status, TaskStatus.NEEDS_REVIEW)
        self.assertEqual(self.svc.pending_review_transfers(), [task.task_id])
        self.svc.register_route("route-4", "zone-4", "shelter-2", primary=True)
        outcome = self.svc.review_task(task.task_id, self.gridder2)
        self.assertEqual(outcome["route_id"], "route-4")
        self.assertEqual(self.svc.tasks[task.task_id].status, TaskStatus.PENDING)
        self.assertEqual(self.svc.pending_review_transfers(), [])


class AlertReceiptTest(ServiceCase):
    def test_alert_covers_points_and_risk_zones(self) -> None:
        deadline = self.clock.moment + timedelta(hours=1)
        result = self.svc.issue_alert("camp-1", self.commander, "暴雨预警", deadline)
        self.assertEqual(result["target_group_ids"], ["group-1", "group-2", "group-3"])
        scoped = self.svc.issue_alert("camp-2", self.commander, "沟口注意", deadline, ["zone-2"])
        self.assertEqual(scoped["target_group_ids"], ["group-2"])

    def test_receipt_idempotent_and_overdue_survives_recovery(self) -> None:
        deadline = self.clock.moment + timedelta(hours=1)
        self.svc.issue_alert("camp-1", self.commander, "暴雨预警", deadline)
        first = self.svc.record_receipt("camp-1", "group-1", "rcpt-1", self.gridder1)
        self.assertFalse(first["duplicated"])
        again = self.svc.record_receipt("camp-1", "group-1", "rcpt-1", self.gridder1)
        self.assertTrue(again["duplicated"])
        other_id = self.svc.record_receipt("camp-1", "group-1", "rcpt-1b", self.gridder1)
        self.assertTrue(other_id["duplicated"])
        with self.assertRaises(ConflictError):
            self.svc.record_receipt("camp-1", "group-2", "rcpt-1", self.gridder1)
        receipts = [e for e in self.svc.store.load() if e.type == "receipt_recorded"]
        self.assertEqual(len(receipts), 1)
        self.clock.advance(hours=2)
        overdue = self.svc.overdue_alerts()
        self.assertEqual(overdue[0]["missing_group_ids"], ["group-2", "group-3"])
        self.reopen()
        overdue = self.svc.overdue_alerts()
        self.assertEqual(overdue[0]["missing_group_ids"], ["group-2", "group-3"])
        self.svc.record_receipt("camp-1", "group-2", "rcpt-2", self.gridder1)
        self.assertEqual(self.svc.overdue_alerts()[0]["missing_group_ids"], ["group-3"])


class TransferTest(ServiceCase):
    def test_arrival_dedup_and_dual_confirmation(self) -> None:
        self.escalate()
        task_id = "transfer:order:assess-1:group-1"
        self.svc.report_departure(task_id, self.gridder1)
        self.assertEqual(self.svc.persons["p-1"].status, PersonStatus.EN_ROUTE)
        first = self.svc.record_arrival(task_id, "arr-1", ["p-1"], self.staff1)
        self.assertEqual(first["newly_safe"], ["p-1"])
        self.assertEqual(self.svc.shelters["shelter-1"].occupancy, 1)
        again = self.svc.record_arrival(task_id, "arr-1", ["p-1"], self.staff1)
        self.assertTrue(again["duplicated"])
        repeat = self.svc.record_arrival(task_id, "arr-2", ["p-1", "p-2"], self.staff1)
        self.assertEqual(repeat["already_safe"], ["p-1"])
        self.assertEqual(repeat["newly_safe"], ["p-2"])
        self.assertEqual(self.svc.shelters["shelter-1"].occupancy, 2)
        self.assertEqual(self.svc.tasks[task_id].status, TaskStatus.AWAITING_CONFIRM)
        with self.assertRaises(SeparationOfDutiesError):
            self.svc.confirm_task(task_id, self.commander)
        self.svc.confirm_task(task_id, self.gridder1, "全员到达")
        self.assertEqual(self.svc.tasks[task_id].status, TaskStatus.CONFIRMED)

    def test_verify_task_also_requires_other_confirmer(self) -> None:
        self.escalate()
        with self.assertRaises(SeparationOfDutiesError):
            self.svc.confirm_task("verify:assess-1:zone-1", self.commander)
        self.svc.confirm_task("verify:assess-1:zone-1", self.gridder1, "核查无异常")
        self.assertEqual(self.svc.tasks["verify:assess-1:zone-1"].status, TaskStatus.CONFIRMED)

    def test_arrival_capacity_enforced(self) -> None:
        self.svc.register_shelter("shelter-6", "临时安置点", 3)
        self.svc.register_route("route-8", "zone-1", "shelter-6")
        self.svc.register_route("route-9", "zone-2", "shelter-6")
        self.svc.issue_transfer_order(
            "o-3", self.commander, [{"group_id": "group-1", "route_id": "route-8", "shelter_id": "shelter-6"}]
        )
        self.svc.issue_transfer_order(
            "o-4", self.commander, [{"group_id": "group-2", "route_id": "route-9", "shelter_id": "shelter-6"}]
        )
        self.svc.record_arrival("transfer:o-3:group-1", "arr-1", ["p-1", "p-2"], self.staff1)
        with self.assertRaises(CapacityError):
            self.svc.record_arrival("transfer:o-4:group-2", "arr-2", ["p-3", "p-4"], self.staff1)

    def test_double_order_for_same_group_rejected(self) -> None:
        self.escalate()
        with self.assertRaises(ConflictError):
            self.svc.issue_transfer_order(
                "o-9", self.commander, [{"group_id": "group-1", "route_id": "route-1", "shelter_id": "shelter-1"}]
            )

    def test_refusal_records_fact_and_triggers_persuasion(self) -> None:
        self.escalate(scope=["zone-2"])
        task_id = "transfer:order:assess-1:group-2"
        result = self.svc.report_refusal(task_id, "ref-1", self.gridder1, "老人不愿离家")
        follow_up = self.svc.tasks[result["follow_up_task_id"]]
        self.assertEqual(follow_up.kind, TaskKind.PERSUADE)
        self.assertEqual(self.svc.tasks[task_id].status, TaskStatus.REFUSED)
        self.assertEqual(self.svc.persons["p-3"].status, PersonStatus.REFUSED)
        unaccounted = {p["person_id"] for p in self.svc.unaccounted_persons()}
        self.assertIn("p-3", unaccounted)
        again = self.svc.report_refusal(task_id, "ref-1", self.gridder1, "老人不愿离家")
        self.assertTrue(again["duplicated"])
        with self.assertRaises(SeparationOfDutiesError):
            self.svc.confirm_task(follow_up.task_id, self.gridder1)
        self.svc.confirm_task(follow_up.task_id, self.commander, "已同意转移")

    def test_comm_outage_records_fact_and_triggers_door_check(self) -> None:
        result = self.svc.report_comm_outage("out-1", "group-3", self.gridder2, "景区信号中断")
        follow_up = self.svc.tasks[result["follow_up_task_id"]]
        self.assertEqual(follow_up.kind, TaskKind.DOOR_CHECK)
        self.assertEqual(self.svc.persons["p-5"].status, PersonStatus.UNREACHABLE)
        unaccounted = {p["person_id"]: p["status"] for p in self.svc.unaccounted_persons()}
        self.assertEqual(unaccounted["p-5"], PersonStatus.UNREACHABLE)

    def test_route_closure_assigns_alternative(self) -> None:
        self.escalate(scope=["zone-2"])
        task_id = "transfer:order:assess-1:group-2"
        result = self.svc.close_route("route-2", self.gridder1, "塌方")
        self.assertEqual(result["reassigned_task_ids"], [task_id])
        task = self.svc.tasks[task_id]
        self.assertEqual((task.route_id, task.shelter_id), ("route-2b", "shelter-2"))
        self.assertEqual(self.svc.routes["route-2"].status, RouteStatus.CLOSED)

    def test_route_closure_without_alternative_flags_review(self) -> None:
        self.escalate()
        task_id = "transfer:order:assess-1:group-1"
        result = self.svc.close_route("route-1", self.gridder1, "泥石流")
        self.assertEqual(result["flagged_task_ids"], [task_id])
        self.assertEqual(self.svc.tasks[task_id].status, TaskStatus.NEEDS_REVIEW)
        with self.assertRaises(InvalidStateError):
            self.svc.report_departure(task_id, self.gridder1)
        self.svc.reopen_route("route-1", self.gridder1)
        outcome = self.svc.review_task(task_id, self.gridder1)
        self.assertEqual(outcome["route_id"], "route-1")
        self.assertEqual(self.svc.tasks[task_id].status, TaskStatus.PENDING)


class AccessTest(ServiceCase):
    def test_sensitive_roster_only_for_grid_and_shelter_posts(self) -> None:
        self.escalate()
        roster = self.svc.group_roster(self.gridder1, "group-1")
        self.assertEqual(roster[0]["name"], "张三")
        self.assertEqual(roster[0]["phone"], "13800000001")
        commander_view = self.svc.group_roster(self.commander, "group-1")
        self.assertEqual(commander_view[0]["name"], "张*")
        self.assertEqual(commander_view[0]["phone"], "138****01")
        other_grid = self.svc.group_roster(self.gridder2, "group-1")
        self.assertEqual(other_grid[0]["name"], "张*")
        shelter_view = self.svc.group_roster(self.staff1, "group-1")
        self.assertEqual(shelter_view[0]["name"], "张三")
        unrelated = self.svc.group_roster(self.staff1, "group-3")
        self.assertEqual(unrelated[0]["name"], "游*")


class LateRainfallTest(ServiceCase):
    def test_late_rainfall_changes_future_measures_not_history(self) -> None:
        self.svc.record_rainfall("rain-1", "station-1", self.clock.moment, 60.0, self.gridder1)
        self.clock.advance(minutes=30)
        self.svc.assess_risk("assess-1", self.commander, 2, ["zone-1"], {"rainfall_ids": ["rain-1"]})
        self.svc.issue_transfer_order(
            "o-1", self.commander, [{"group_id": "group-2", "route_id": "route-2", "shelter_id": "shelter-1"}]
        )
        before = self.svc.store.load()
        late_observed = self.clock.moment - timedelta(minutes=20)
        result = self.svc.record_rainfall("rain-2", "station-1", late_observed, 120.0, self.gridder1)
        self.assertTrue(result["late"])
        escalated = self.svc.assess_risk(
            "assess-2", self.commander, 4, ["zone-1", "zone-2", "zone-3"], {"rainfall_ids": ["rain-2"]}
        )
        self.assertTrue(escalated["escalation"])
        self.assertEqual(escalated["skipped_group_ids"], ["group-1", "group-2"])
        after = self.svc.store.load()
        self.assertEqual(after[: len(before)], before)
        order_event = next(e for e in after if e.type == "transfer_ordered" and e.payload["order_id"] == "o-1")
        self.assertEqual(order_event.payload["issuer"], "cmdr-1")
        self.assertEqual(self.svc.risk_level, 4)


class TeamMaterialTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.svc.register_team("team-1", "山城救援队", "重庆", 20)
        self.svc.register_material("batch-1", "帐篷", 100, "重庆")

    def test_team_cannot_be_double_deployed(self) -> None:
        result = self.svc.deploy_team("dep-1", "team-1", "陕西", "沟口搜救", self.commander)
        self.assertTrue(result["cross_district"])
        with self.assertRaises(DoubleAllocationError):
            self.svc.deploy_team("dep-2", "team-1", "四川", "增援", self.commander)
        self.svc.recall_team("dep-1", self.commander)
        redeploy = self.svc.deploy_team("dep-3", "team-1", "重庆", "就地前置", self.commander)
        self.assertFalse(redeploy["cross_district"])

    def test_material_hold_dispatch_receive_no_double_allocation(self) -> None:
        self.svc.hold_material("h-1", "batch-1", 30, "支援陕西", self.logistics)
        self.assertEqual(self.svc.batch_balance("batch-1")["available"], 70)
        with self.assertRaises(InsufficientStockError):
            self.svc.hold_material("h-2", "batch-1", 80, "超占", self.logistics)
        self.svc.hold_material("h-3", "batch-1", 10, "本区前置", self.logistics)
        with self.assertRaises(InvalidStateError):
            self.svc.receive_hold("h-3", self.logistics)
        self.svc.dispatch_hold("h-1", self.logistics)
        self.assertTrue(self.svc.dispatch_hold("h-1", self.logistics)["duplicated"])
        self.assertEqual(self.svc.pending_handover_holds(), ["h-1"])
        self.svc.receive_hold("h-1", self.logistics)
        self.assertTrue(self.svc.receive_hold("h-1", self.logistics)["duplicated"])
        balance = self.svc.batch_balance("batch-1")
        self.assertEqual((balance["received"], balance["held"], balance["available"]), (30, 10, 60))
        self.svc.release_hold("h-3", self.logistics)
        self.assertEqual(self.svc.batch_balance("batch-1")["available"], 70)
        self.assertEqual(self.svc.holds["h-1"].status, HoldStatus.RECEIVED)


class RecoveryTest(ServiceCase):
    def test_recovery_continues_overdue_review_and_handover(self) -> None:
        deadline = self.clock.moment + timedelta(minutes=30)
        self.svc.issue_alert("camp-9", self.commander, "暴雨预警", deadline)
        self.escalate()
        self.svc.close_route("route-1", self.gridder1, "泥石流")
        self.svc.register_material("batch-9", "棉被", 50, "重庆")
        self.svc.hold_material("h-9", "batch-9", 20, "安置点补充", self.logistics)
        self.svc.dispatch_hold("h-9", self.logistics)
        self.clock.advance(hours=1)
        self.reopen()
        report = self.svc.handover_report()
        self.assertEqual([item["campaign_id"] for item in report["overdue_alerts"]], ["camp-9"])
        self.assertEqual(report["pending_review_transfers"], ["transfer:order:assess-1:group-1"])
        self.assertEqual(report["pending_handover_holds"], ["h-9"])
        self.assertEqual(len(report["unaccounted_persons"]), 6)
        self.svc.record_receipt("camp-9", "group-1", "rcpt-9", self.gridder1)
        self.svc.reopen_route("route-1", self.gridder1)
        self.svc.review_task("transfer:order:assess-1:group-1", self.gridder1)
        self.svc.receive_hold("h-9", self.logistics)
        report = self.svc.handover_report()
        self.assertEqual(report["pending_review_transfers"], [])
        self.assertEqual(report["pending_handover_holds"], [])
        self.assertEqual(report["overdue_alerts"][0]["missing_group_ids"], ["group-2", "group-3"])


class DeactivationTest(ServiceCase):
    def test_reconstruct_at_deactivation(self) -> None:
        self.escalate(scope=["zone-1", "zone-2"])
        self.svc.record_arrival("transfer:order:assess-1:group-1", "arr-1", ["p-1", "p-2"], self.staff1)
        self.svc.confirm_task("transfer:order:assess-1:group-1", self.gridder1)
        self.svc.register_team("team-1", "山城救援队", "重庆", 20)
        self.svc.deploy_team("dep-1", "team-1", "陕西", "沟口搜救", self.commander)
        self.svc.register_material("batch-1", "帐篷", 100, "重庆")
        self.svc.hold_material("h-1", "batch-1", 30, "支援陕西", self.logistics)
        self.svc.dispatch_hold("h-1", self.logistics)
        self.svc.receive_hold("h-1", self.logistics)
        self.svc.close_route("route-2", self.gridder1, "塌方")
        self.svc.deactivate_response("deact-1", self.commander, "降雨减弱，响应解除")
        with self.assertRaises(InvalidStateError):
            self.svc.record_rainfall("rain-x", "station-1", self.clock.moment, 1.0, self.gridder1)
        with self.assertRaises(InvalidStateError):
            self.svc.register_grid("grid-9", "新网格", [])
        snapshot = self.svc.reconstruct_at("deact-1")
        self.assertEqual(snapshot["risk"]["level"], 3)
        self.assertEqual(snapshot["persons"]["by_status"].get(PersonStatus.SAFE), 2)
        self.assertEqual(snapshot["routes"]["route-2"], RouteStatus.CLOSED)
        self.assertEqual(snapshot["teams"]["team-1"]["status"], "deployed")
        self.assertEqual(snapshot["materials"]["batch-1"]["received"], 30)
        kinds = {(item["kind"], item["id"]) for item in snapshot["approvals"]}
        self.assertIn(("risk_assessment", "assess-1"), kinds)
        self.assertIn(("transfer_order", "order:assess-1"), kinds)
        issuer = {item["id"]: item["issued_by"] for item in snapshot["approvals"]}
        self.assertEqual(issuer["order:assess-1"], "cmdr-1")
        self.assertEqual(snapshot["deactivation"]["open_items"]["unaccounted_person_ids"], ["p-3", "p-4", "p-5", "p-6"])
        with self.assertRaises(NotFoundError):
            self.svc.reconstruct_at("deact-unknown")


class RegistrationTest(ServiceCase):
    def test_identical_registration_idempotent_conflict_rejected(self) -> None:
        again = self.svc.register_grid("grid-1", "沟口网格", ["gridder-1"])
        self.assertTrue(again["duplicated"])
        with self.assertRaises(ConflictError):
            self.svc.register_grid("grid-1", "改名网格", ["gridder-1"])

    def test_unknown_references_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.register_zone("zone-9", ZoneKind.HAZARD_POINT, "未知", "grid-9")
        with self.assertRaises(InvalidStateError):
            self.svc.register_zone("zone-9", "unknown-kind", "未知", "grid-1")


if __name__ == "__main__":
    unittest.main()
