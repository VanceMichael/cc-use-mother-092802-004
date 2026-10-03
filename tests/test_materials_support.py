"""物资批次、前置、预留、领用、交接与跨区支援：杜绝双重占用。"""

import unittest

from src.geohazard.errors import GeoHazardError
from src.geohazard.service import Command

from scenario import T0, at, build_minimal
from src.geohazard.timeutil import add_minutes


def _log(minutes: int, actor: str = "保障员-高越") -> Command:
    return Command(actor, "logistics", add_minutes(T0, minutes))


class MaterialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_minimal()
        self.svc.receive_batch(at(10, role="logistics", actor="保障员-高越"),
                               "B1", "毛毯", 100, "条")

    def test_reservation_reduces_available_but_not_on_hand(self) -> None:
        svc = self.svc
        self.assertEqual(svc.proj.stock_available("WAREHOUSE", "B1"), 100)
        svc.reserve_material(_log(20), "RSV-1", "WAREHOUSE", "B1", 30, "任务预留")
        self.assertEqual(svc.proj.stock_on_hand("WAREHOUSE", "B1"), 100)
        self.assertEqual(svc.proj.stock_available("WAREHOUSE", "B1"), 70)
        # 超量预留被拒（不能对同一批库存双重占用）
        with self.assertRaisesRegex(GeoHazardError, "可占用量不足"):
            svc.reserve_material(_log(21), "RSV-2", "WAREHOUSE", "B1", 71, "再次预留")

    def test_issue_consumes_stock_and_reservation(self) -> None:
        svc = self.svc
        svc.reserve_material(_log(20), "RSV-1", "WAREHOUSE", "B1", 30)
        svc.issue_material(_log(25), "RSV-1")
        self.assertEqual(svc.proj.stock_on_hand("WAREHOUSE", "B1"), 70)
        self.assertEqual(svc.proj.stock_available("WAREHOUSE", "B1"), 70)
        # 同一预留单不能重复领用
        with self.assertRaisesRegex(GeoHazardError, "不可领用"):
            svc.issue_material(_log(26), "RSV-1")
        # 释放已发放预留被拒
        with self.assertRaisesRegex(GeoHazardError, "不在有效状态"):
            svc.release_reservation(_log(27), "RSV-1")

    def test_preposition_moves_stock_and_cannot_overshoot(self) -> None:
        svc = self.svc
        svc.preposition_stock(_log(20), "B1", 40, "SH1")
        self.assertEqual(svc.proj.stock_on_hand("SH1", "B1"), 40)
        self.assertEqual(svc.proj.stock_on_hand("WAREHOUSE", "B1"), 60)
        with self.assertRaisesRegex(GeoHazardError, "超过现有库存"):
            svc.preposition_stock(_log(21), "B1", 61, "SH1")

    def test_handover_freezes_location_until_accepted(self) -> None:
        svc = self.svc
        svc.preposition_stock(_log(20), "B1", 40, "SH1")
        svc.handover_stock(_log(30), "HO-1", "SH1", "甲", "乙")
        with self.assertRaisesRegex(GeoHazardError, "交接"):
            svc.preposition_stock(_log(31), "B1", 1, "SH1", from_location="WAREHOUSE")
        with self.assertRaisesRegex(GeoHazardError, "冻结"):
            svc.reserve_material(_log(32), "RSV-X", "SH1", "B1", 1)
        # 账实相符后接收，冻结解除
        svc.accept_handover(Command("乙", "logistics", add_minutes(T0, 40)), "HO-1")
        svc.reserve_material(_log(41), "RSV-2", "SH1", "B1", 10)

    def test_handover_rejected_when_books_do_not_match_goods(self) -> None:
        svc = self.svc
        svc.preposition_stock(_log(20), "B1", 40, "SH1")
        svc.handover_stock(_log(30), "HO-1", "SH1", "甲", "乙")
        # 交接期间库存被异常动用（直接构造事件模拟账实差异）
        svc.proj.stock["SH1"]["B1"] -= 5
        with self.assertRaisesRegex(GeoHazardError, "账实不符"):
            svc.accept_handover(Command("乙", "logistics", add_minutes(T0, 40)), "HO-1")


class CrossRegionSupportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_minimal()
        self.svc.receive_batch(at(10, role="logistics", actor="保障员-高越"),
                               "B1", "饮用水", 100, "箱")
        self.svc.define_team(at(11), "TM-X", "邻县队", "邻水县")

    def test_offered_support_freezes_source_stock(self) -> None:
        svc = self.svc
        svc.offer_support(at(50, actor="邻水县指挥员"), "SUP-1", "TM-X",
                          "邻水县", "峡江县", "B1", 50)
        # 源端已冻结 50，再为本地任务预留超过 50 即失败
        with self.assertRaisesRegex(GeoHazardError, "可占用量不足"):
            svc.reserve_material(
                Command("保障员-高越", "logistics", add_minutes(T0, 51)),
                "RSV-LOCAL", "WAREHOUSE", "B1", 51,
            )

    def test_release_unfreezes_without_double_counting(self) -> None:
        svc = self.svc
        svc.offer_support(at(50, actor="邻水县指挥员"), "SUP-1", "TM-X",
                          "邻水县", "峡江县", "B1", 50)
        svc.release_support(at(55, actor="邻水县指挥员"), "SUP-1")
        self.assertEqual(svc.proj.stock_available("WAREHOUSE", "B1"), 100)
        # 已撤回的支援单不能再次撤回或接收
        with self.assertRaises(GeoHazardError):
            svc.release_support(at(56, actor="邻水县指挥员"), "SUP-1")
        with self.assertRaises(GeoHazardError):
            svc.accept_support(at(56), "SUP-1")

    def test_accept_issues_once_and_team_not_double_assigned(self) -> None:
        svc = self.svc
        svc.offer_support(at(50, actor="邻水县指挥员"), "SUP-2", "TM-X",
                          "邻水县", "峡江县", "B1", 50)
        svc.accept_support(at(55), "SUP-2")
        # 物资已从源端出库（50 箱在途，仅挂支援单），源端可用=50
        self.assertEqual(svc.proj.stock_on_hand("WAREHOUSE", "B1"), 50)
        # 队伍已派遣，不能再次前置或派遣
        with self.assertRaisesRegex(GeoHazardError, "已在任务中"):
            svc.preposition_team(at(60), "TM-X", "Z1")


if __name__ == "__main__":
    unittest.main()
