"""追加式事件存储：响应过程的全部事实按序落盘，支持恢复与回放。

事件一旦写入不可修改：迟到的雨情只能以新事件追加，不能改写
当时已经发出的命令；系统恢复后按序号回放即可还原状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Event:
    """一条已经落盘的事实。"""

    seq: int
    type: str
    payload: dict[str, Any]
    recorded_at: str


class EventStore:
    """以 JSONL 形式只追加存储事件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._seq: int | None = None

    def append(self, event_type: str, payload: dict[str, Any], recorded_at: str) -> Event:
        if self._seq is None:
            self._seq = len(self.load())
        self._seq += 1
        event = Event(seq=self._seq, type=event_type, payload=payload, recorded_at=recorded_at)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "seq": event.seq,
                "type": event.type,
                "payload": event.payload,
                "recorded_at": event.recorded_at,
            },
            ensure_ascii=False,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return event

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                events.append(
                    Event(
                        seq=raw["seq"],
                        type=raw["type"],
                        payload=raw["payload"],
                        recorded_at=raw["recorded_at"],
                    )
                )
        return events
