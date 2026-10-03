"""端到端种子场景：点区双控、四类风险区、解除还原全部贯通。"""

import tempfile
import unittest
from pathlib import Path

from src.geohazard import access
from src.geohazard.archive import restore_at_closure, verify_journal
from src.geohazard.events import EventStore
from src.geohazard.recovery import recover
from src.geohazard.seed import build


class SeedScenarioTest(unittest.TestCase):
    def _build(self, path: Path):
        return build(EventStore(path))

    def test_full_scenario_reaches_closure_with_everyone_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._build(Path(tmp) / "journal.jsonl")
            board = svc.dashboard()
            self.assertEqual(board["people_total"], 6)
            self.assertEqual(board["safe"], 6)
            self.assertEqual(board["tasks_completed"], 4)
            self.assertEqual(board["tasks_open"], 0)
            self.assertEqual(board["alternatives_open"], 0)
            self.assertIsNone(board["response"])  # 已解除
            # 四类风险区全覆盖
            self.assertEqual(board["zones"], 4)
            kinds = {z["kind"] for z in svc.proj.zones.values()}
            self.assertEqual(
                kinds,
                {"gully_mouth", "cliff_slope", "construction_site", "scenic_area"},
            )
            # 安置点容量占用不重复计数
            self.assertEqual(svc.proj.shelters["SH-1"]["occupancy"], 3)
            self.assertEqual(svc.proj.shelters["SH-2"]["occupancy"], 3)
            verify_journal(svc.store)

    def test_recovered_journal_replays_identically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            svc = self._build(path)
            revived = recover(path)
            self.assertEqual(revived.dashboard(), svc.dashboard())

    def test_closure_restore_keeps_refusal_closure_and_basis_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._build(Path(tmp) / "journal.jsonl")
            arc = restore_at_closure(svc.store, "RESP-2026-0712")
            people = arc["replay_snapshot"]["people"]
            # 曾拒绝转移者最终安全，但拒绝事实仍在
            self.assertTrue(
                any(f["kind"] == "refusal" for f in people["P-003"]["facts"])
            )
            self.assertEqual(people["P-003"]["status"], "safe")
            # 曾失联游客的失联事实仍在
            self.assertTrue(
                any(f["kind"] == "contact_lost" for f in people["P-101"]["facts"])
            )
            # 封闭路线保持封闭事实
            self.assertEqual(arc["replay_snapshot"]["routes"]["RT-1"]["status"], "closed")
            # 批准依据链含两次研判及其雨情
            levels = [a["level"] for a in arc["basis_chain"]]
            self.assertEqual(levels, ["yellow", "orange"])

    def test_sensitive_data_scoped_in_full_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._build(Path(tmp) / "journal.jsonl")
            g1_view = next(
                p for p in svc.persons_for(access.GRID, "G1") if p["person_id"] == "P-002"
            )
            self.assertEqual(g1_view["care_need"], "高血压随药")  # 本网格可见
            g2_view = next(
                p for p in svc.persons_for(access.GRID, "G2") if p["person_id"] == "P-002"
            )
            self.assertIn("*", g2_view["care_need"])  # 跨网格掩码


class TaskGenerationIdempotencyTest(unittest.TestCase):
    def test_regenerating_tasks_after_restart_does_not_duplicate(self) -> None:
        from scenario import at, build_minimal, issue_orange

        svc = build_minimal()
        issue_orange(svc)
        events_before = len(svc.store.events)
        # 同一研判再次生成任务：同点区已有在办任务且等级未变，不产生任何事件
        svc.generate_tasks(at(40), "RA-O-1")
        self.assertEqual(len(svc.store.events), events_before)
        self.assertEqual(sorted(svc.proj.tasks), ["TASK-Z1"])


if __name__ == "__main__":
    unittest.main()
