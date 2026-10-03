"""任务闭环：发布指令者不能独自确认完成，须独立第二人会签。"""

import unittest

from src.geohazard.errors import GeoHazardError

from scenario import at, build_minimal, issue_orange
from test_calls_transfer import _grid, _rescue, _shelter


def _make_everyone_safe(svc, people: int) -> None:
    for i in range(1, people + 1):
        pid = f"P-{i:02d}"
        svc.confirm_receipt(_grid(35), "TASK-Z1", pid, idem_key=f"recv-{pid}")
        svc.report_arrival(_rescue(40 + i), pid, "SH1", idem_key=f"arr-{pid}")
    for i in range(1, people + 1):
        svc.check_in_shelter(_shelter(50 + i), f"P-{i:02d}", "SH1")


class TaskSignOffTest(unittest.TestCase):
    def test_issuer_alone_cannot_complete_task(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        _make_everyone_safe(svc, 1)
        # 发令人就是 at() 的默认 actor "指挥员-林岚"
        svc.sign_task_completed(at(60), "TASK-Z1")
        self.assertEqual(svc.task_status("TASK-Z1")["status"], "open")
        # 发令人再签一次无效（同一人不得重复签字）
        with self.assertRaisesRegex(GeoHazardError, "同一人"):
            svc.sign_task_completed(at(61), "TASK-Z1")
        # 独立网格员会签后闭环
        svc.sign_task_completed(_grid(62), "TASK-Z1")
        self.assertEqual(svc.task_status("TASK-Z1")["status"], "completed")

    def test_completion_blocked_while_someone_unaccounted(self) -> None:
        svc = build_minimal(people=2)
        issue_orange(svc)
        # 仅 P-01 安全，P-02 无回执、无替代方案
        svc.confirm_receipt(_grid(35), "TASK-Z1", "P-01", idem_key="recv-1")
        svc.report_arrival(_rescue(40), "P-01", "SH1", idem_key="arr-1")
        svc.check_in_shelter(_shelter(45), "P-01", "SH1")
        with self.assertRaisesRegex(GeoHazardError, "未确认安全"):
            svc.sign_task_completed(at(50), "TASK-Z1")

    def test_two_independent_commanders_also_satisfy_dual_control(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        _make_everyone_safe(svc, 1)
        svc.sign_task_completed(at(60, actor="指挥员-林岚"), "TASK-Z1")
        svc.sign_task_completed(at(61, actor="值班长-冯海"), "TASK-Z1")
        self.assertEqual(svc.task_status("TASK-Z1")["status"], "completed")

    def test_signed_task_cannot_be_signed_again(self) -> None:
        svc = build_minimal(people=1)
        issue_orange(svc)
        _make_everyone_safe(svc, 1)
        svc.sign_task_completed(at(60), "TASK-Z1")
        svc.sign_task_completed(_grid(62), "TASK-Z1")
        with self.assertRaisesRegex(GeoHazardError, "已闭环"):
            svc.sign_task_completed(at(63, actor="值班长-冯海"), "TASK-Z1")


if __name__ == "__main__":
    unittest.main()
