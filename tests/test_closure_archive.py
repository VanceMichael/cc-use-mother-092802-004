"""解除响应：双批准、任务清零、时点还原与批准依据链。"""

import unittest

from src.geohazard.archive import restore_at_closure, verify_journal
from src.geohazard.errors import GeoHazardError

from scenario import at, build_minimal, issue_orange
from test_calls_transfer import _grid, _rescue, _shelter


def _complete_response(svc, people: int = 1) -> None:
    issue_orange(svc)
    for i in range(1, people + 1):
        pid = f"P-{i:02d}"
        svc.confirm_receipt(_grid(35), "TASK-Z1", pid, idem_key=f"recv-{pid}")
        svc.report_arrival(_rescue(40 + i), pid, "SH1", idem_key=f"arr-{pid}")
    for i in range(1, people + 1):
        svc.check_in_shelter(_shelter(50 + i), f"P-{i:02d}", "SH1")
    svc.sign_task_completed(at(60), "TASK-Z1")
    svc.sign_task_completed(_grid(61), "TASK-Z1")


class ClosureTest(unittest.TestCase):
    def test_closure_requires_two_approvers_and_open_tasks_closed(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        # 任务未闭环不能解除
        with self.assertRaisesRegex(GeoHazardError, "未闭环"):
            svc.close_response(at(70), "RESP-1")
        # 即使有两名批准人，任务未闭环仍被拦截
        svc.approve_closure(at(70, actor="应急局长-周鼎"), "RESP-1")
        svc.approve_closure(at(71, actor="副指挥-秦昭"), "RESP-1")
        with self.assertRaisesRegex(GeoHazardError, "未闭环"):
            svc.close_response(at(72, actor="应急局长-周鼎"), "RESP-1")

    def test_first_approver_cannot_be_responder_starter(self) -> None:
        svc = build_minimal(people=1)
        _complete_response(svc)
        # at() 默认 actor 即启动者本人
        with self.assertRaisesRegex(GeoHazardError, "第一名批准人不能是响应启动者"):
            svc.approve_closure(at(70), "RESP-1")
        svc.approve_closure(at(70, actor="应急局长-周鼎"), "RESP-1", "同意")
        with self.assertRaisesRegex(GeoHazardError, "两名"):
            svc.close_response(at(71, actor="应急局长-周鼎"), "RESP-1")
        svc.approve_closure(at(72, actor="副指挥-秦昭"), "RESP-1", "复核同意")
        result = svc.close_response(at(75, actor="应急局长-周鼎"), "RESP-1")
        self.assertIn("snapshot", result)
        self.assertIsNone(svc.proj.active_response())

    def test_same_approver_cannot_approve_twice(self) -> None:
        svc = build_minimal(people=1)
        _complete_response(svc)
        svc.approve_closure(at(70, actor="应急局长-周鼎"), "RESP-1")
        with self.assertRaisesRegex(GeoHazardError, "重复批准"):
            svc.approve_closure(at(71, actor="应急局长-周鼎"), "RESP-1")

    def test_restore_reproduces_state_at_closure_with_facts_and_basis(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.confirm_receipt(_grid(35), "TASK-Z1", "P-01", idem_key="recv-1")
        svc.record_refusal(_grid(36), "TASK-Z1", "P-01", "犹豫")
        svc.start_transfer(_grid(45), "TASK-Z1", "P-01")
        svc.report_arrival(_rescue(46), "P-01", "SH1", idem_key="arr-1")
        svc.check_in_shelter(_shelter(47), "P-01", "SH1")
        svc.close_alternative(_grid(48), "ALT-REFUSE-TASK-Z1-P-01", "劝离成功")
        svc.sign_task_completed(at(60), "TASK-Z1")
        svc.sign_task_completed(_grid(61), "TASK-Z1")
        svc.approve_closure(at(70, actor="应急局长-周鼎"), "RESP-1")
        svc.approve_closure(at(71, actor="副指挥-秦昭"), "RESP-1")
        svc.close_response(at(75, actor="应急局长-周鼎"), "RESP-1")

        arc = restore_at_closure(svc.store, "RESP-1")
        snap = arc["replay_snapshot"]
        self.assertEqual(snap["people"]["P-01"]["status"], "safe")
        self.assertTrue(
            any(f["kind"] == "refusal" for f in snap["people"]["P-01"]["facts"])
        )
        self.assertEqual(snap["assessments"][0]["basis_reports"], ["RAIN-1"])
        # 批准依据链保留雨情的观测时间与录入时间
        report = arc["basis_chain"][0]["reports"][0]
        self.assertNotEqual(report["issued_at"], report["recorded_ts"])
        self.assertEqual([a["approver"] for a in arc["approvals"]],
                         ["应急局长-周鼎", "副指挥-秦昭"])
        # 解除之后新增的事件不影响还原
        events_before = len(svc.store.events)
        self.assertEqual(
            restore_at_closure(svc.store, "RESP-1")["replay_snapshot"]["people"]["P-01"]["status"],
            "safe",
        )
        self.assertEqual(len(svc.store.events), events_before)

    def test_journal_verification_reports_tampering(self) -> None:
        svc = build_minimal(people=1)
        _complete_response(svc)
        report = verify_journal(svc.store)
        self.assertEqual(report["events"], len(svc.store.events))
        self.assertEqual(len(report["fingerprints"]), len(svc.store.events))
        # 篡改某条事件载荷后指纹变化（只追加账本本身不提供修改入口）
        evt = svc.store.events[0]
        evt.payload["level"] = 1
        self.assertNotEqual(
            __import__("src.geohazard.archive", fromlist=["event_fingerprint"]).event_fingerprint(evt),
            report["fingerprints"][0]["fingerprint"],
        )

    def test_closed_response_cannot_be_closed_again(self) -> None:
        svc = build_minimal(people=1)
        _complete_response(svc)
        svc.approve_closure(at(70, actor="应急局长-周鼎"), "RESP-1")
        svc.approve_closure(at(71, actor="副指挥-秦昭"), "RESP-1")
        svc.close_response(at(75, actor="应急局长-周鼎"), "RESP-1")
        with self.assertRaisesRegex(GeoHazardError, "已解除"):
            svc.close_response(at(76, actor="应急局长-周鼎"), "RESP-1")


if __name__ == "__main__":
    unittest.main()
