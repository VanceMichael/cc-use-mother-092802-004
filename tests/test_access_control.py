"""敏感人员信息只向相应网格和安置岗位开放。"""

import unittest

from src.geohazard import access
from src.geohazard.errors import PermissionDeniedError

from scenario import at, build_minimal, issue_orange
from test_calls_transfer import _rescue, _shelter


class AccessControlTest(unittest.TestCase):
    def test_sensitive_fields_masked_outside_grid_and_shelter(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.report_arrival(_rescue(40), "P-01", "SH1", idem_key="arr-1")
        svc.check_in_shelter(_shelter(45), "P-01", "SH1")

        # 指挥人员可见完整信息
        commander_view = svc.persons_for(access.COMMANDER)[0]
        self.assertTrue(commander_view["id_number"].startswith("510222"))
        # 本网格员可见
        grid_view = next(p for p in svc.persons_for(access.GRID, "G1") if p["person_id"] == "P-01")
        self.assertEqual(grid_view["phone"], "13800000001")
        # 其他网格员不可见 -> 掩码
        other_grid = next(p for p in svc.persons_for(access.GRID, "G9") if p["person_id"] == "P-01")
        self.assertIn("*", other_grid["id_number"])
        self.assertNotEqual(other_grid["phone"], "13800000001")
        # 非到达安置点岗位不可见
        other_shelter = next(
            p for p in svc.persons_for(access.SHELTER, "SH-OTHER") if p["person_id"] == "P-01"
        )
        self.assertIn("*", other_shelter["care_need"])
        # 实际安置点岗位可见照护信息
        own_shelter = next(
            p for p in svc.persons_for(access.SHELTER, "SH1") if p["person_id"] == "P-01"
        )
        self.assertEqual(own_shelter["care_need"], "慢性病随药")

    def test_observer_gets_only_masked_summary_data(self) -> None:
        svc = build_minimal(people=1)
        view = svc.persons_for(access.OBSERVER)[0]
        self.assertIn("*", view["id_number"])

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            access.require_role("mayor", access.COMMANDER)

    def test_rescue_cannot_view_sensitive_fields(self) -> None:
        svc = build_minimal(people=1)
        view = svc.persons_for(access.RESCUE, "TM-1")[0]
        self.assertIn("*", view["phone"])

    def test_shelter_role_scoped_to_own_shelter_for_checkin(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        svc.report_arrival(_rescue(40), "P-01", "SH1", idem_key="arr-1")
        from src.geohazard.service import Command

        wrong = Command("shelter:SH9", "shelter", at(45).ts)
        with self.assertRaises(PermissionDeniedError):
            svc.check_in_shelter(wrong, "P-01", "SH1")


if __name__ == "__main__":
    unittest.main()
