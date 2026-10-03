"""端到端演练种子：全部为虚构数据，不含真实个人信息。

场景脉络（虚构区县"峡江县"）：
1. 四级响应启动；登记沟口、临崖陡坡、工地、景区四类风险区与网格。
2. 雨情到来，黄色研判生成核查预警任务；橙色升级追加转移任务。
3. 叫应：大部分回执；一名住户拒绝、一名游客通信中断，均开立替代方案。
4. 主路线封闭，启用备用路线；拒绝者被劝离、失联者由队伍找到后转移。
5. 安置点复核入住；发令人独自签字不能闭环，独立网格员会签后闭环。
6. 队伍前置、物资批次前置；跨区支援物资在源端冻结不双重占用。
7. 库存交接、解除双批准与时点还原。

用法：
    python -m src.geohazard.seed fixtures/journal.seed.jsonl
"""

from __future__ import annotations

import sys
from pathlib import Path

from .events import EventStore
from .service import Command, ResponseService

T0 = "2026-07-12 06:00:00"


def _min(ts: str, minutes: int) -> str:
    from .timeutil import add_minutes

    return add_minutes(ts, minutes)


def build(store: EventStore) -> ResponseService:
    svc = ResponseService(store)
    commander = Command("指挥员-林岚", "commander", T0)
    g1 = lambda m: Command("grid:G1", "grid", m)  # 沟口村网格员岗位账号
    g2 = lambda m: Command("grid:G2", "grid", m)  # 崖畔景区网格员岗位账号
    sh = lambda m, sid: Command(f"shelter:{sid}", "shelter", m)
    log = lambda m: Command("保障员-高越", "logistics", m)

    # 1. 启动响应
    svc.open_response(
        commander, "RESP-2026-0712", 4, "峡江县",
        "国家地质灾害四级应急响应，持续强降雨",
    )

    # 2. 网格、四类风险区、安置点与路线
    svc.define_grid(commander, "G1", "沟口村网格")
    svc.define_grid(commander, "G2", "崖畔景区网格")
    svc.register_zone(commander, "Z-GULLY", "松树沟沟口", "gully_mouth", "G1")
    svc.define_shelter(commander, "SH-1", "沟口村集中安置点", 200)
    svc.define_shelter(commander, "SH-2", "景区游客中心安置点", 300)
    svc.define_route(commander, "RT-1", "SH-1", ["Z-GULLY"], "沟口主路")
    svc.register_zone(commander, "Z-CLIFF", "老鹰崖临崖陡坡", "cliff_slope", "G2")
    svc.register_zone(commander, "Z-SITE", "崖畔公路施工工地", "construction_site", "G2")
    svc.register_zone(commander, "Z-SCENIC", "崖畔景区", "scenic_area", "G2")
    svc.define_route(commander, "RT-2", "SH-2", ["Z-CLIFF", "Z-SITE", "Z-SCENIC"], "景区通道")
    svc.define_route(commander, "RT-2B", "SH-2", ["Z-CLIFF", "Z-SITE", "Z-SCENIC"], "景区备用通道")

    # 3. 住户与游客分组、人员（虚构身份号与电话）
    svc.register_group(commander, "GRP-G1", "resident", "Z-GULLY", "G1", "沟口村住户一组")
    svc.register_group(commander, "GRP-SCENIC", "tourist", "Z-SCENIC", "G2", "景区滞留游客")
    svc.register_group(commander, "GRP-SITE", "worker", "Z-SITE", "G2", "工地工人")
    svc.register_person(
        commander, "P-001", "王*安", "GRP-G1",
        id_number="51022219800101001X", phone="13800000001",
    )
    svc.register_person(
        commander, "P-002", "李*秀", "GRP-G1",
        id_number="51022219820304002X", phone="13800000002", care_need="高血压随药",
    )
    svc.register_person(commander, "P-003", "陈*强", "GRP-G1")  # 后续拒绝转移
    svc.register_person(commander, "P-101", "游客-赵*", "GRP-SCENIC")  # 后续通信中断
    svc.register_person(commander, "P-102", "游客-钱*", "GRP-SCENIC")
    svc.register_person(commander, "P-201", "工人-孙*", "GRP-SITE")

    # 物资批次先期入库（保障准备先于雨情升级）
    svc.receive_batch(log(_min(T0, 15)), "B-WATER", "瓶装饮用水", 500, "箱")
    svc.receive_batch(log(_min(T0, 16)), "B-BLANKET", "毛毯", 300, "条")

    # 4. 雨情与黄色研判 -> 核查与叫应任务
    svc.record_rain_report(
        Command("指挥员-林岚", "commander", _min(T0, 20)),
        "RAIN-01", "松树沟站", _min(T0, 10), 86.0, 22.0,
    )
    svc.issue_risk_assessment(
        Command("指挥员-林岚", "commander", _min(T0, 25)),
        "RA-Y-1", "yellow", ["Z-GULLY"], ["RAIN-01"], "沟口降雨明显，先核查叫应",
    )
    svc.generate_tasks(Command("指挥员-林岚", "commander", _min(T0, 30)), "RA-Y-1")

    # 沟口住户叫应回执（含一次重复回执演示幂等）
    m = _min(T0, 40)
    for pid in ("P-001", "P-002", "P-003"):
        svc.attempt_call(g1(m), "TASK-Z-GULLY", pid, "phone", "振铃接通")
        svc.confirm_receipt(g1(m), "TASK-Z-GULLY", pid, idem_key=f"recv-{pid}-1")
    dup = svc.confirm_receipt(
        g1(_min(T0, 41)), "TASK-Z-GULLY", "P-001", idem_key="recv-P-001-1"
    )
    assert dup["idempotent"] is True

    # 5. 橙色升级（四个风险区全部纳入）-> 追加转移任务
    svc.record_rain_report(
        Command("指挥员-林岚", "commander", _min(T0, 50)),
        "RAIN-02", "崖畔站", _min(T0, 45), 152.0, 41.0,
    )
    svc.issue_risk_assessment(
        Command("指挥员-林岚", "commander", _min(T0, 55)),
        "RA-O-1", "orange",
        ["Z-GULLY", "Z-CLIFF", "Z-SITE", "Z-SCENIC"],
        ["RAIN-01", "RAIN-02"],
        "雨强加大，橙色风险，受威胁区域组织转移",
    )
    svc.generate_tasks(Command("指挥员-林岚", "commander", _min(T0, 60)), "RA-O-1")

    # 6. 队伍与物资前置
    svc.define_team(
        Command("指挥员-林岚", "commander", _min(T0, 61)),
        "TM-1", "县综合救援队一队", "峡江县",
    )
    svc.preposition_team(
        Command("指挥员-林岚", "commander", _min(T0, 62)),
        "TM-1", "Z-SCENIC", "TASK-Z-SCENIC",
    )
    svc.preposition_stock(log(_min(T0, 65)), "B-WATER", 100, "SH-2")
    svc.preposition_stock(log(_min(T0, 66)), "B-BLANKET", 120, "SH-1")
    svc.reserve_material(
        log(_min(T0, 67)), "RSV-TM1", "SH-2", "B-WATER", 20, "景区游客保障",
    )

    # 7. 各点区叫应与转移
    # 沟口：P-003 拒绝转移
    svc.record_refusal(
        g1(_min(T0, 70)), "TASK-Z-GULLY", "P-003", "担心家中牲畜，不愿离开",
    )
    # 景区：P-101 无法接通；P-102 正常；工地 P-201 正常
    svc.attempt_call(g2(_min(T0, 70)), "TASK-Z-SCENIC", "P-101", "phone", "无法接通")
    svc.attempt_call(g2(_min(T0, 70)), "TASK-Z-SCENIC", "P-102", "phone", "接通")
    svc.confirm_receipt(
        g2(_min(T0, 70)), "TASK-Z-SCENIC", "P-102", idem_key="recv-P-102-1",
    )
    svc.attempt_call(g2(_min(T0, 70)), "TASK-Z-SITE", "P-201", "radio", "对讲机确认")
    svc.confirm_receipt(
        g2(_min(T0, 70)), "TASK-Z-SITE", "P-201", idem_key="recv-P-201-1",
    )
    # 复叫仍无应答，登记通信中断并开立替代方案
    svc.attempt_call(g2(_min(T0, 74)), "TASK-Z-SCENIC", "P-101", "satphone", "仍无法接通")
    svc.report_contact_lost(
        g2(_min(T0, 75)), "TASK-Z-SCENIC", "P-101", last_channel="satphone",
    )

    # 8. 主路线封闭：自动开立替代方案；开辟并改挂备用路线
    svc.close_route(
        Command("TM-1 领队", "rescue", _min(T0, 80)),
        "RT-1", "沟口主路K2处边坡溜塌，道路中断",
    )
    svc.define_route(
        Command("指挥员-林岚", "commander", _min(T0, 81)),
        "RT-1B", "SH-1", ["Z-GULLY"], "沟口机耕道备用线",
    )
    svc.open_alternative(
        Command("指挥员-林岚", "commander", _min(T0, 82)),
        "ALT-GULLY-BACKUP",
        reason="RT-1封闭后沟口住户转移通道调整",
        measure="改走机耕道备用路线 RT-1B，救援队接引",
        task_id="TASK-Z-GULLY",
        old_route_id="RT-1",
        new_route_id="RT-1B",
    )
    svc.reroute_task(
        Command("指挥员-林岚", "commander", _min(T0, 83)),
        "TASK-Z-GULLY", "RT-1B", "RT-1边坡溜塌，改走机耕道",
    )

    # P-001/P-002 经备用路线转移并入住（重复报到演示不重复计数）
    for pid, minute in (("P-001", 95), ("P-002", 97)):
        svc.report_arrival(
            Command("TM-1 队员", "rescue", _min(T0, minute)), pid, "SH-1",
            idem_key=f"arr-{pid}",
        )
    for pid, minute in (("P-001", 100), ("P-002", 102)):
        svc.check_in_shelter(sh(_min(T0, minute), "SH-1"), pid, "SH-1")
    dup_arr = svc.report_arrival(
        Command("TM-1 队员", "rescue", _min(T0, 96)), "P-001", "SH-1",
        idem_key="arr-P-001",
    )
    assert dup_arr["idempotent"] is True and dup_arr["counted"] is False

    # 拒绝者二次劝离成功，随救援队转移
    svc.start_transfer(g1(_min(T0, 105)), "TASK-Z-GULLY", "P-003")
    svc.report_arrival(
        Command("TM-1 队员", "rescue", _min(T0, 110)), "P-003", "SH-1",
        idem_key="arr-P-003",
    )
    svc.check_in_shelter(sh(_min(T0, 115), "SH-1"), "P-003", "SH-1")
    svc.close_alternative(
        g1(_min(T0, 116)), "ALT-REFUSE-TASK-Z-GULLY-P-003", "二次劝离成功，已安全入住SH-1",
    )
    svc.close_alternative(
        g1(_min(T0, 117)), "ALT-ROUTE-RT-1-TASK-Z-GULLY", "启用RT-1B完成转移",
    )
    svc.close_alternative(
        Command("指挥员-林岚", "commander", _min(T0, 118)),
        "ALT-GULLY-BACKUP", "机耕道备用线通行，全员转移完毕",
    )
    # RT-1 保持封闭事实留痕，不抹除；不重新开放。

    # 失联游客由救援队实地找到，与其余景区/工地人员一并经景区备用通道转移
    for pid, minute in (("P-101", 120), ("P-102", 121), ("P-201", 122)):
        svc.report_arrival(
            Command("TM-1 队员", "rescue", _min(T0, minute)), pid, "SH-2",
            idem_key=f"arr-{pid}",
        )
    for pid, minute in (("P-101", 125), ("P-102", 126), ("P-201", 127)):
        svc.check_in_shelter(sh(_min(T0, minute), "SH-2"), pid, "SH-2")
    svc.close_alternative(
        g2(_min(T0, 128)), "ALT-LOST-TASK-Z-SCENIC-P-101", "敲门寻人找到，随队转移入住",
    )

    # 9. 双岗确认：发令人独自签字无效，独立网格员会签闭环
    svc.sign_task_completed(
        Command("指挥员-林岚", "commander", _min(T0, 130)), "TASK-Z-SITE",
    )
    svc.sign_task_completed(g2(_min(T0, 131)), "TASK-Z-SITE")

    # 10. 跨区支援（物资源端冻结，撤回释放，不双重占用）
    svc.define_team(
        Command("指挥员-林岚", "commander", _min(T0, 139)),
        "TM-X", "邻县支援队", "邻水县",
    )
    offer = svc.offer_support(
        Command("邻水县指挥员", "commander", _min(T0, 140)),
        "SUP-1", "TM-X", "邻水县", "峡江县",
        material_batch_id="B-WATER", material_qty=50,
    )
    # 注意：支援从源端 WAREHOUSE 冻结；此处演示受援仓库视角不受影响
    svc.release_support(
        Command("邻水县指挥员", "commander", _min(T0, 145)), "SUP-1",
    )
    # 再次发起并接收，物资正式启运
    offer2 = svc.offer_support(
        Command("邻水县指挥员", "commander", _min(T0, 150)),
        "SUP-2", "TM-X", "邻水县", "峡江县",
        material_batch_id="B-WATER", material_qty=50,
    )
    svc.accept_support(
        Command("指挥员-林岚", "commander", _min(T0, 155)), "SUP-2",
    )

    # 11. 库存交接（系统恢复后仍可继续接收）
    svc.handover_stock(
        log(_min(T0, 160)), "HO-SH1", "SH-1", "保障员-高越", "安置点管理员-何苗",
    )

    # 闭环其余任务
    for i, (tid, grid_cmd) in enumerate(
        (
            ("TASK-Z-GULLY", g1),
            ("TASK-Z-CLIFF", g2),
            ("TASK-Z-SCENIC", g2),
        )
    ):
        base = 170 + i * 3
        svc.sign_task_completed(
            Command("指挥员-林岚", "commander", _min(T0, base)), tid,
        )
        svc.sign_task_completed(grid_cmd(_min(T0, base + 1)), tid)

    # 接收交接
    svc.accept_handover(
        Command("安置点管理员-何苗", "logistics", _min(T0, 180)), "HO-SH1",
    )

    # 12. 解除响应：两名指挥人员批准（启动者不得作为第一批准人）
    svc.approve_closure(
        Command("应急局长-周鼎", "commander", _min(T0, 183)),
        "RESP-2026-0712", "风险减弱，全员安全，同意解除",
    )
    svc.approve_closure(
        Command("副指挥-秦昭", "commander", _min(T0, 185)),
        "RESP-2026-0712", "复核同意",
    )
    svc.close_response(
        Command("应急局长-周鼎", "commander", _min(T0, 190)), "RESP-2026-0712",
    )
    return svc


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("fixtures/journal.seed.jsonl")
    if path.exists():
        path.unlink()
    store = EventStore(path)
    svc = build(store)
    board = svc.dashboard()
    print(f"已生成演练账本: {path}（{len(store.events)} 条事件）")
    print(f"安全人数: {board['safe']}/{board['people_total']}，"
          f"任务闭环: {board['tasks_completed']}，响应: 已解除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
