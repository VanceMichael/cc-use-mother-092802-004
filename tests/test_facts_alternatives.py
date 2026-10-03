"""拒绝转移、通信中断、路线封闭：事实保留且触发替代方案。"""

import unittest

from src.geohazard.errors import GeoHazardError

from scenario import at, build_minimal, issue_orange
from test_calls_transfer import _grid, _rescue, _shelter


class FactsAndAlternativesTest(unittest.TestCase):
    def test_refusal_keeps_fact_and_blocks_completion_until_resolved(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.confirm_receipt(_grid(35), "TASK-Z1", "P-01", idem_key="recv-1")
        svc.record_refusal(_grid(36), "TASK-Z1", "P-01", "不愿离开")

        # 事实保留
        self.assertTrue(svc.proj.people["P-01"]["refused"])
        self.assertTrue(svc.proj.people["P-01"]["facts"])
        # 自动开立替代方案，且替代方案未闭环前任何签字都不能闭环任务
        alts = svc.task_status("TASK-Z1")["open_alternatives"]
        self.assertTrue(any("REFUSE" in a for a in alts))
        with self.assertRaisesRegex(GeoHazardError, "替代方案"):
            svc.sign_task_completed(at(40), "TASK-Z1")
        with self.assertRaisesRegex(GeoHazardError, "替代方案"):
            svc.sign_task_completed(_grid(41), "TASK-Z1")

        # 劝离成功、转移入住、闭环替代方案后任务可完成
        svc.start_transfer(_grid(45), "TASK-Z1", "P-01")
        svc.report_arrival(_rescue(46), "P-01", "SH1", idem_key="arr-1")
        svc.check_in_shelter(_shelter(47), "P-01", "SH1")
        svc.close_alternative(
            _grid(48), "ALT-REFUSE-TASK-Z1-P-01", "二次劝离成功",
        )
        # 发令人与独立网格员双岗会签闭环
        svc.sign_task_completed(at(49), "TASK-Z1")
        svc.sign_task_completed(_grid(50), "TASK-Z1")
        status = svc.task_status("TASK-Z1")
        self.assertEqual(status["status"], "completed")

    def test_contact_lost_triggers_search_alternative(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.attempt_call(_grid(35), "TASK-Z1", "P-01", "phone", "无法接通")
        svc.report_contact_lost(_grid(40), "TASK-Z1", "P-01", last_channel="phone")
        self.assertTrue(svc.proj.people["P-01"]["contact_lost"])
        alts = svc.task_status("TASK-Z1")["open_alternatives"]
        self.assertTrue(any("LOST" in a for a in alts))

    def test_route_closure_opens_alternatives_and_requires_reroute(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.close_route(_rescue(35), "RT1", "边坡溜塌")
        # 在途/待办任务自动出现替代方案
        self.assertIn("ALT-ROUTE-RT1-TASK-Z1", svc.proj.alternatives)
        # 原路线不可用，直接开始转移被拒绝
        with self.assertRaisesRegex(GeoHazardError, "路线不可用"):
            svc.start_transfer(_grid(36), "TASK-Z1", "P-01")
        # 开辟备用线并正式改道（产生事件，重放不丢）
        svc.define_route(at(37), "RT1-B", "SH1", ["Z1"], "机耕道备用线")
        svc.reroute_task(at(38), "TASK-Z1", "RT1-B", "主路中断")
        svc.start_transfer(_grid(39), "TASK-Z1", "P-01")
        self.assertEqual(svc.proj.tasks["TASK-Z1"]["route_id"], "RT1-B")

    def test_cannot_reroute_to_closed_or_unrelated_route(self) -> None:
        svc = build_minimal()
        issue_orange(svc)
        svc.define_route(at(31), "RT-OTHER", "SH1", ["Z-OTHER"], "别处路线")
        # Z-OTHER 未登记
        # 先登记一个不相关点区
        svc.define_grid(at(32), "G2", "别处网格")
        svc.register_zone(at(33), "Z9", "别处沟口", "gully_mouth", "G2")
        svc.define_route(at(34), "RT9", "SH1", ["Z9"], "别处开放线")
        with self.assertRaisesRegex(GeoHazardError, "不覆盖"):
            svc.reroute_task(at(35), "TASK-Z1", "RT9")

    def test_closed_route_fact_remains_in_history(self) -> None:
        svc = build_minimal()
        svc.close_route(at(20), "RT1", "落石")
        route = svc.proj.routes["RT1"]
        self.assertEqual(route["status"], "closed")
        self.assertEqual([h["status"] for h in route["history"]], ["open", "closed"])


if __name__ == "__main__":
    unittest.main()
