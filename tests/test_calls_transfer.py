"""叫应、转移与唯一安全计数：重复回执幂等、已安全者不重复计数。"""

import unittest

from src.geohazard.errors import GeoHazardError, PermissionDeniedError

from scenario import at, build_minimal, issue_orange


def _grid(minutes: int, grid: str = "G1"):
    from src.geohazard.service import Command

    return Command(f"grid:{grid}", "grid", at(minutes).ts)


def _shelter(minutes: int, shelter: str = "SH1"):
    from src.geohazard.service import Command

    return Command(f"shelter:{shelter}", "shelter", at(minutes).ts)


def _rescue(minutes: int):
    from src.geohazard.service import Command

    return Command("救援队-甲", "rescue", at(minutes).ts)


class CallAndTransferTest(unittest.TestCase):
    def test_duplicate_receipt_is_idempotent_and_counts_once(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.attempt_call(_grid(35), "TASK-Z1", "P-01", "phone", "接通")
        first = svc.confirm_receipt(_grid(36), "TASK-Z1", "P-01", idem_key="recv-p01-1")
        second = svc.confirm_receipt(_grid(37), "TASK-Z1", "P-01", idem_key="recv-p01-1")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["event_seq"], first["event_seq"])
        progress = svc.task_status("TASK-Z1")["progress"]
        self.assertEqual(progress["warned"], 1)

    def test_arrival_and_checkin_never_double_count(self) -> None:
        svc = build_minimal(people=2)
        issue_orange(svc)
        for i, pid in enumerate(("P-01", "P-02"), start=1):
            svc.confirm_receipt(_grid(34 + i), "TASK-Z1", pid, idem_key=f"recv-{pid}")
        for i, pid in enumerate(("P-01", "P-02"), start=1):
            svc.report_arrival(_rescue(38 + i), pid, "SH1", idem_key=f"arr-{pid}")
        for i, pid in enumerate(("P-01", "P-02"), start=1):
            svc.check_in_shelter(_shelter(42 + i), pid, "SH1")
        # 同一人重复报到、重复入住
        dup = svc.report_arrival(_rescue(46), "P-01", "SH1", idem_key="arr-P-01")
        self.assertTrue(dup["idempotent"])
        self.assertFalse(dup["counted"])
        again = svc.check_in_shelter(_shelter(47), "P-01", "SH1")
        self.assertTrue(again["idempotent"])
        self.assertEqual(svc.proj.shelters["SH1"]["occupancy"], 2)
        self.assertEqual(svc.dashboard()["safe"], 2)

    def test_shelter_capacity_is_enforced(self) -> None:
        svc = build_minimal(people=1)
        svc.register_person(at(9), "P-03", "村民03", "GRP1")
        svc.define_shelter(at(10), "SH3", "满员点", 1)
        svc.report_arrival(_rescue(40), "P-01", "SH3", idem_key="arr-p01-sh3")
        # 第二个人无法进入容量 1 且已有 1 人到达待复核的点
        with self.assertRaisesRegex(GeoHazardError, "容量不足"):
            svc.report_arrival(_rescue(41), "P-03", "SH3", idem_key="arr-p03")

    def test_zero_capacity_shelter_is_rejected(self) -> None:
        svc = build_minimal()
        with self.assertRaisesRegex(GeoHazardError, "容量必须为正"):
            svc.define_shelter(at(8), "SH2", "非法点", 0)

    def test_grid_worker_can_only_handle_own_grid(self) -> None:
        svc = build_minimal()
        issue_orange(svc)
        from src.geohazard.service import Command

        other = Command("grid:G9", "grid", at(35).ts)
        with self.assertRaises(PermissionDeniedError):
            svc.attempt_call(other, "TASK-Z1", "P-01", "phone", "x")

    def test_overdue_call_appears_in_resume_list(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)  # 任务办理时限 30 分钟（从 30 分起）
        svc.attempt_call(_grid(35), "TASK-Z1", "P-01", "phone", "无人接听")
        overdue = svc.proj.overdue_calls(at(65).ts)
        self.assertEqual([item["person_id"] for item in overdue], ["P-01"])
        # 补叫成功后不再逾期
        svc.confirm_receipt(_grid(66), "TASK-Z1", "P-01", idem_key="recv-late")
        self.assertEqual(svc.proj.overdue_calls(at(67).ts), [])


if __name__ == "__main__":
    unittest.main()
