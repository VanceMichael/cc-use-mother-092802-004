"""系统恢复：重启后续办逾期叫应、待复核转移与库存交接。"""

import tempfile
import unittest
from pathlib import Path

from src.geohazard.events import EventStore
from src.geohazard.recovery import recover, resume_work
from src.geohazard.service import ResponseService

from scenario import at
from test_calls_transfer import _grid, _rescue
from test_materials_support import _log


class RecoveryTest(unittest.TestCase):
    def _journal_with_pending_work(self, path: Path) -> None:
        store = EventStore(path)
        svc = ResponseService(store)
        svc.open_response(at(0), "RESP-1", 4, "峡江县", "持续强降雨")
        svc.define_grid(at(1), "G1", "沟口网格")
        svc.register_zone(at(2), "Z1", "沟口", "gully_mouth", "G1")
        svc.define_shelter(at(3), "SH1", "安置点", 50)
        svc.define_route(at(4), "RT1", "SH1", ["Z1"])
        svc.register_group(at(5), "GRP1", "resident", "Z1", "G1")
        # 一批物资先期入库
        svc.receive_batch(at(6, role="logistics", actor="保障员"), "B1", "毛毯", 30, "条")
        svc.register_person(at(7), "P-01", "村民甲", "GRP1")
        svc.register_person(at(8), "P-02", "村民乙", "GRP1")
        svc.record_rain_report(at(10), "R1", "站", at(8).ts, 160.0, 45.0)
        svc.issue_risk_assessment(at(12), "RA-O", "orange", ["Z1"], ["R1"])
        svc.generate_tasks(at(15), "RA-O")  # 截止 06:45
        # P-01 已到达但未复核入住；P-02 完全未叫应
        svc.confirm_receipt(_grid(20), "TASK-Z1", "P-01", idem_key="recv-1")
        svc.report_arrival(_rescue(25), "P-01", "SH1", idem_key="arr-1")
        svc.handover_stock(_log(30), "HO-1", "WAREHOUSE", "保障员", "接班人")

    def test_recovery_lists_all_three_pending_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            self._journal_with_pending_work(path)

            svc = recover(path)
            pending = resume_work(svc, "2026-07-12 07:00:00")
            self.assertEqual(
                [c["person_id"] for c in pending["overdue_calls"]], ["P-02"],
            )
            self.assertEqual(
                [v["person_id"] for v in pending["pending_verifications"]], ["P-01"],
            )
            self.assertEqual(
                [h["handover_id"] for h in pending["pending_handovers"]], ["HO-1"],
            )

            # 续办：补叫应、复核入住、接收交接
            svc.confirm_receipt(_grid(61), "TASK-Z1", "P-02", idem_key="recv-2")
            from test_calls_transfer import _shelter

            svc.check_in_shelter(_shelter(62), "P-01", "SH1")
            from src.geohazard.service import Command

            svc.accept_handover(
                Command("接班人", "logistics", "2026-07-12 07:05:00"),
                "HO-1",
            )
            pending2 = resume_work(svc, "2026-07-12 07:10:00")
            self.assertEqual(pending2["overdue_calls"], [])
            self.assertEqual(pending2["pending_verifications"], [])
            self.assertEqual(pending2["pending_handovers"], [])

    def test_recovered_state_matches_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            self._journal_with_pending_work(path)
            svc = recover(path)
            self.assertEqual(svc.dashboard()["people_total"], 2)
            self.assertEqual(svc.proj.shelters["SH1"]["arrived"], 1)


if __name__ == "__main__":
    unittest.main()
