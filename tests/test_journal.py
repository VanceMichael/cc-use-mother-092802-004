"""事件账本：只追加、幂等去重、时间线单调、可重放。"""

import unittest

from src.geohazard.errors import GeoHazardError, IdempotentReplay
from src.geohazard.events import EventStore, event_fingerprint
from src.geohazard.projections import Projection


class JournalTest(unittest.TestCase):
    def test_append_replay_and_sequence(self) -> None:
        store = EventStore.memory()
        e1 = store.append(ts="2026-07-12 06:00:00", actor="甲", role="commander",
                          etype="ResponseOpened",
                          payload={"response_id": "R1", "level": 4, "region": "峡江县",
                                   "basis": "强降雨"})
        e2 = store.append(ts="2026-07-12 06:05:00", actor="甲", role="commander",
                          etype="RainReportRecorded",
                          payload={"report_id": "W1", "station": "站", "issued_at": "2026-07-12 06:04:00",
                                   "recorded_ts": "2026-07-12 06:05:00", "late": False,
                                   "cumulative_mm": 80.0, "intensity_mm_h": 20.0})
        self.assertEqual([e1.seq, e2.seq], [1, 2])
        rebuilt = Projection.rebuild(store.replay())
        self.assertIn("R1", rebuilt.responses)
        self.assertIn("W1", rebuilt.reports)

    def test_idempotent_key_returns_original_event(self) -> None:
        store = EventStore.memory()
        first = store.append(ts="2026-07-12 06:00:00", actor="网", role="grid",
                             etype="CallReceiptConfirmed", payload={"person_id": "P1"},
                             idem_key="recv-1")
        with self.assertRaises(IdempotentReplay) as ctx:
            store.append(ts="2026-07-12 06:01:00", actor="网", role="grid",
                         etype="CallReceiptConfirmed", payload={"person_id": "P1"},
                         idem_key="recv-1")
        self.assertEqual(ctx.exception.receipt.seq, first.seq)
        self.assertEqual(len(store.events), 1)

    def test_timeline_must_be_monotonic(self) -> None:
        store = EventStore.memory()
        store.append(ts="2026-07-12 06:00:00", actor="甲", role="commander",
                     etype="ResponseOpened", payload={})
        with self.assertRaisesRegex(GeoHazardError, "早于账本最新时间"):
            store.append(ts="2026-07-12 05:59:00", actor="甲", role="commander",
                         etype="RainReportRecorded", payload={})

    def test_fingerprint_stable_after_later_events(self) -> None:
        store = EventStore.memory()
        first = store.append(ts="2026-07-12 06:00:00", actor="甲", role="commander",
                             etype="ResponseOpened", payload={"level": 4})
        fp_before = event_fingerprint(first)
        store.append(ts="2026-07-12 08:00:00", actor="乙", role="commander",
                     etype="RainReportRecorded", payload={"late": True})
        self.assertEqual(event_fingerprint(first), fp_before)


if __name__ == "__main__":
    unittest.main()
