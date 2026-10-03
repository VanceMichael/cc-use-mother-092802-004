"""响应服务测试公共构造：一个最小可运行的四级响应场景。"""

from __future__ import annotations

from src.geohazard.events import EventStore
from src.geohazard.service import Command, ResponseService

T0 = "2026-07-12 06:00:00"


def at(minutes: int, actor: str = "指挥员-林岚", role: str = "commander") -> Command:
    from src.geohazard.timeutil import add_minutes

    return Command(actor, role, add_minutes(T0, minutes))


def build_minimal(store: EventStore | None = None, *, people: int = 1) -> ResponseService:
    """响应启动 + 网格/风险区/安置点/路线/分组/人员（人员编号 P-01..）。"""
    svc = ResponseService(store or EventStore.memory())
    svc.open_response(at(0), "RESP-1", 4, "峡江县", "持续强降雨四级响应")
    svc.define_grid(at(1), "G1", "沟口村网格")
    svc.register_zone(at(2), "Z1", "松树沟沟口", "gully_mouth", "G1")
    svc.define_shelter(at(3), "SH1", "沟口安置点", 50)
    svc.define_route(at(4), "RT1", "SH1", ["Z1"], "沟口主路")
    svc.register_group(at(5), "GRP1", "resident", "Z1", "G1", "住户一组")
    for i in range(1, people + 1):
        svc.register_person(
            at(6 + i), f"P-{i:02d}", f"村民{i:02d}", "GRP1",
            id_number=f"5102221980010100{i}X", phone=f"1380000000{i}",
            care_need="慢性病随药" if i == 1 else "",
        )
    return svc


def issue_orange(svc: ResponseService, minutes: int = 30, zones: list[str] | None = None) -> str:
    svc.record_rain_report(at(minutes - 10), "RAIN-1", "松树沟站", at(minutes - 12).ts, 150.0, 40.0)
    assessment_id = "RA-O-1"
    svc.issue_risk_assessment(
        at(minutes - 5), assessment_id, "orange", zones or ["Z1"], ["RAIN-1"], "橙色风险",
    )
    svc.generate_tasks(at(minutes), assessment_id)
    return assessment_id
