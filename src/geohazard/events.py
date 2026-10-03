"""只追加事件账本。

设计要点：
- 事件一经写入不可修改、不可删除（迟到雨情只能产生新事件，不能改写旧命令）。
- 每条命令以 ``(命令类型, 幂等键)`` 去重：同一键重放直接返回原回执，不追加事件。
- 每条事件携带全局序号、时间与操作者；账本写入与投影重放分离，
  系统恢复后从文件继续，逾期叫应、待复核转移和库存交接都能续办。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import GeoHazardError, IdempotentReplay


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    actor: str
    role: str
    etype: str
    payload: dict[str, Any]
    command_id: str = ""
    idem_key: str = ""
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "role": self.role,
            "etype": self.etype,
            "command_id": self.command_id,
            "idem_key": self.idem_key,
            "basis": self.basis,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"],
            ts=raw["ts"],
            actor=raw["actor"],
            role=raw["role"],
            etype=raw["etype"],
            payload=raw["payload"],
            command_id=raw.get("command_id", ""),
            idem_key=raw.get("idem_key", ""),
            basis=raw.get("basis", ""),
        )


def event_fingerprint(evt: Event) -> str:
    """命令事件内容指纹：解除归档时可证明当时命令未被事后改写。"""
    material = json.dumps(
        {
            "seq": evt.seq,
            "ts": evt.ts,
            "actor": evt.actor,
            "etype": evt.etype,
            "command_id": evt.command_id,
            "basis": evt.basis,
            "payload": evt.payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EventStore:
    """JSONL 文件账本；内存场景可用 ``memory()`` 构造。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._events: list[Event] = []
        self._idem_index: dict[tuple[str, str], int] = {}
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._ingest(Event.from_dict(json.loads(line)))

    @classmethod
    def memory(cls) -> "EventStore":
        return cls(None)

    def _ingest(self, evt: Event) -> None:
        self._events.append(evt)
        if evt.idem_key:
            self._idem_index.setdefault((evt.etype, evt.idem_key), evt.seq)

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def replay(self) -> Iterable[Event]:
        return iter(self._events)

    def replay_until_seq(self, seq_exclusive: int) -> Iterable[Event]:
        """解除时点还原：只回放指定序号之前的事件。"""
        for evt in self._events:
            if evt.seq >= seq_exclusive:
                break
            yield evt

    def replay_until(self, closed_ts: str) -> Iterable[Event]:
        """解除时点还原：回放首个匹配时间的解除命令（不含）之前的事件。"""
        for evt in self._events:
            if evt.etype == "ResponseClosed" and evt.ts == closed_ts:
                break
            yield evt

    def lookup_idem(self, etype: str, idem_key: str) -> Event | None:
        seq = self._idem_index.get((etype, idem_key))
        return None if seq is None else self._events[seq - 1]

    def append(
        self,
        *,
        ts: str,
        actor: str,
        role: str,
        etype: str,
        payload: dict[str, Any],
        command_id: str = "",
        idem_key: str = "",
        basis: str = "",
    ) -> Event:
        if idem_key:
            prior = self.lookup_idem(etype, idem_key)
            if prior is not None:
                raise IdempotentReplay(prior)
        if self._events and ts < self._events[-1].ts:
            raise GeoHazardError(
                f"命令时间 {ts} 早于账本最新时间 {self._events[-1].ts}；"
                "迟到命令须记录实际到达时间，原始签发时间保留在载荷中"
            )
        evt = Event(
            seq=len(self._events) + 1,
            ts=ts,
            actor=actor,
            role=role,
            etype=etype,
            payload=payload,
            command_id=command_id,
            idem_key=idem_key,
            basis=basis,
        )
        self._ingest(evt)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")
        return evt

    @property
    def next_seq(self) -> int:
        return len(self._events) + 1
