"""解除响应归档与时点还原。

从某次 ResponseClosed 可以还原：
- 当时的雨情依据与逐级风险研判；
- 点区清单、责任网格、住户/游客分组与每个人的安全状态；
- 转移路线开闭事实与替代方案；
- 救援队伍部署、跨区支援与物资库存；
- 双岗批准记录与每条命令的不可改写指纹。
"""

from __future__ import annotations

from typing import Any

from .events import event_fingerprint
from .projections import Projection


def restore_at_closure(store: EventStore, response_id: str) -> dict[str, Any]:
    """重放至指定响应解除命令（不含），重建解除当时的态势。"""
    close_evt = next(
        (
            e
            for e in store.events
            if e.etype == "ResponseClosed" and e.payload["response_id"] == response_id
        ),
        None,
    )
    if close_evt is None:
        raise KeyError(f"响应 {response_id} 无解除记录")
    proj = Projection.rebuild(store.replay_until_seq(close_evt.seq))
    record = store.events[close_evt.seq - 1].payload.get("snapshot", {})
    return {
        "response_id": response_id,
        "closed_ts": close_evt.ts,
        "closed_by": close_evt.actor,
        "recorded_snapshot": record,
        "replay_snapshot": {
            "assessments": [
                {
                    "id": a["assessment_id"],
                    "level": a["level"],
                    "zones": a["zone_ids"],
                    "basis_reports": a["basis_reports"],
                    "issued_ts": a["issued_ts"],
                }
                for a in proj.assessments.values()
            ],
            "zones": [
                {"zone_id": z["zone_id"], "name": z["name"], "kind": z["kind"],
                 "grid_id": z["grid_id"]}
                for z in proj.zones.values()
            ],
            "groups": [
                {"group_id": g["group_id"], "kind": g["kind"], "zone_id": g["zone_id"]}
                for g in proj.groups.values()
            ],
            "people": {
                pid: {
                    "status": p["status"],
                    "warned": p["warned"],
                    "verified": p["verified"],
                    "arrival_shelter": p["arrival_shelter"],
                    "facts": p["facts"],
                }
                for pid, p in proj.people.items()
            },
            "routes": {
                rid: {"status": r["status"], "shelter_id": r["shelter_id"],
                      "history": r["history"]}
                for rid, r in proj.routes.items()
            },
            "alternatives": [
                {"alt_id": a["alt_id"], "reason": a["reason"], "measure": a["measure"],
                 "status": a["status"]}
                for a in proj.alternatives.values()
            ],
            "teams": {
                tid: {"status": t["status"], "position": t["position"],
                      "assignment": t["assignment"]}
                for tid, t in proj.teams.items()
            },
            "supports": [
                {"support_id": s["support_id"], "team_id": s["team_id"],
                 "from_region": s["from_region"], "to_region": s["to_region"],
                 "material_batch_id": s["material_batch_id"],
                 "material_qty": s["material_qty"], "status": s["status"]}
                for s in proj.supports.values()
            ],
            "stock": {loc: dict(b) for loc, b in proj.stock.items()},
        },
        "approvals": [
            {"approver": a["approver"], "role": a["role"], "ts": a["ts"], "note": a["note"]}
            for a in proj.approvals.get(response_id, [])
        ],
        "basis_chain": _basis_chain(proj),
    }


def _basis_chain(proj: Projection) -> list[dict[str, Any]]:
    """批准依据链：研判 -> 雨情报告 -> 原始签发时间（迟到雨情也如实保留）。"""
    chain = []
    for assess in proj.assessments.values():
        chain.append(
            {
                "assessment_id": assess["assessment_id"],
                "level": assess["level"],
                "issued_ts": assess["issued_ts"],
                "reports": [
                    {
                        "report_id": rid,
                        "station": proj.reports[rid]["station"],
                        "issued_at": proj.reports[rid]["issued_at"],
                        "recorded_ts": proj.reports[rid]["recorded_ts"],
                        "late": proj.reports[rid]["late"],
                    }
                    for rid in assess["basis_reports"]
                ],
            }
        )
    return chain


def verify_journal(store: EventStore) -> dict[str, Any]:
    """核验账本序号连续、时间线单调，并给出每条命令的指纹。"""
    fingerprints = []
    prev_seq = 0
    prev_ts = ""
    for evt in store.events:
        if evt.seq != prev_seq + 1:
            raise ValueError(f"账本序号断裂: 期望 {prev_seq + 1}, 实际 {evt.seq}")
        if prev_ts and evt.ts < prev_ts:
            raise ValueError(f"账本时间线倒退于 seq={evt.seq}")
        prev_seq, prev_ts = evt.seq, evt.ts
        fingerprints.append({"seq": evt.seq, "etype": evt.etype, "fingerprint": event_fingerprint(evt)})
    return {"events": len(store.events), "last_ts": prev_ts, "fingerprints": fingerprints}
