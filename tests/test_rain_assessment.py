"""雨情与风险研判：迟到雨情改变后续措施，但不改写已发命令。"""

import unittest

from src.geohazard.events import event_fingerprint
from src.geohazard.errors import GeoHazardError

from scenario import at, build_minimal


class RainAssessmentTest(unittest.TestCase):
    def _yellow(self, svc, minutes: int = 20) -> None:
        svc.record_rain_report(
            at(minutes - 5), "RAIN-Y", "松树沟站", at(minutes - 6).ts, 60.0, 15.0,
        )
        svc.issue_risk_assessment(
            at(minutes), "RA-Y", "yellow", ["Z1"], ["RAIN-Y"], "黄色预警",
        )
        svc.generate_tasks(at(minutes + 2), "RA-Y")

    def test_late_report_is_flagged_and_escalates_existing_task(self) -> None:
        svc = build_minimal()
        self._yellow(svc)
        task_before = svc.task_status("TASK-Z1")
        self.assertNotIn("transfer", task_before["kinds"])

        # 两小时后才录入的雨情（观测时间远早于录入时间）
        result = svc.record_rain_report(
            at(140), "RAIN-LATE", "松树沟站", at(60).ts, 180.0, 52.0,
        )
        self.assertTrue(result["late"])
        svc.issue_risk_assessment(
            at(145), "RA-R", "red", ["Z1"], ["RAIN-Y", "RAIN-LATE"], "依据迟到雨情升级红色",
        )
        svc.generate_tasks(at(150), "RA-R")

        task_after = svc.task_status("TASK-Z1")
        self.assertEqual(task_after["level"], "red")
        self.assertIn("transfer", task_after["kinds"])

    def test_late_report_appends_event_without_rewriting_prior_command(self) -> None:
        svc = build_minimal()
        self._yellow(svc)
        order = len(svc.store.events)
        assess_evt = next(e for e in svc.store.events if e.etype == "RiskAssessmentIssued")
        fingerprint = event_fingerprint(assess_evt)

        svc.record_rain_report(at(140), "RAIN-LATE", "松树沟站", at(50).ts, 180.0, 52.0)
        self.assertEqual(len(svc.store.events), order + 1)  # 只新增，不修改
        self.assertEqual(event_fingerprint(assess_evt), fingerprint)
        self.assertEqual(assess_evt.payload["level"], "yellow")  # 原研判仍是黄色

    def test_direct_downgrade_is_rejected(self) -> None:
        svc = build_minimal()
        svc.record_rain_report(at(20), "RAIN-1", "站", at(18).ts, 150.0, 40.0)
        svc.issue_risk_assessment(at(25), "RA-O", "orange", ["Z1"], ["RAIN-1"])
        with self.assertRaisesRegex(GeoHazardError, "降级"):
            svc.issue_risk_assessment(at(30), "RA-Y2", "yellow", ["Z1"], ["RAIN-1"])

    def test_assessment_must_reference_real_reports_and_zones(self) -> None:
        svc = build_minimal()
        with self.assertRaises(GeoHazardError):
            svc.issue_risk_assessment(at(25), "RA-X", "orange", ["Z1"], ["NO-SUCH-RAIN"])
        with self.assertRaises(GeoHazardError):
            svc.issue_risk_assessment(at(25), "RA-X", "orange", ["NO-ZONE"], [])


if __name__ == "__main__":
    unittest.main()
