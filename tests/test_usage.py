import json
import unittest
from typing import Any

from software_factory.orchestration.models import UsageAlert
from software_factory.orchestration.usage import (
    UsageWatcher,
    session_usage,
    usage_alert_workflow_id,
)

RESET = 1795440000


def rate_info(**info: Any) -> dict[str, Any]:
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"}


def rate_event(**info: Any) -> str:
    return json.dumps(rate_info(**info)) + "\n"


def unified(utilization: float, resets_at: int = RESET) -> dict[str, Any]:
    return {"five_hour": {"utilization": utilization, "resetsAt": resets_at}}


class TestSessionUsage(unittest.TestCase):
    def test_unified_window(self) -> None:
        event = rate_info(status="allowed", unifiedWindows=unified(0.83))
        self.assertEqual(session_usage(event), UsageAlert("five_hour", 0.83, RESET))

    def test_below_threshold(self) -> None:
        self.assertIsNone(session_usage(rate_info(unifiedWindows=unified(0.79))))
        self.assertEqual(
            session_usage(rate_info(unifiedWindows=unified(0.8))),
            UsageAlert("five_hour", 0.8, RESET),
        )

    def test_top_level_fallback(self) -> None:
        event = rate_info(
            status="allowed_warning", rateLimitType="five_hour", utilization=0.9, resetsAt=RESET
        )
        self.assertEqual(session_usage(event), UsageAlert("five_hour", 0.9, RESET))

    def test_ignores_other_windows(self) -> None:
        top = rate_info(rateLimitType="seven_day", utilization=0.95, resetsAt=RESET)
        self.assertIsNone(session_usage(top))
        weekly = rate_info(
            unifiedWindows={"seven_day": {"utilization": 0.95, "resetsAt": RESET}}
        )
        self.assertIsNone(session_usage(weekly))

    def test_ignores_non_finite_values(self) -> None:
        self.assertIsNone(session_usage(rate_info(unifiedWindows=unified(float("nan")))))
        infinite_reset = rate_info(unifiedWindows=unified(0.9, float("inf")))  # type: ignore[arg-type]
        self.assertIsNone(session_usage(infinite_reset))

    def test_ignores_other_events(self) -> None:
        self.assertIsNone(session_usage({"type": "result", "subtype": "success"}))
        no_utilization = rate_info(rateLimitType="five_hour", resetsAt=RESET, status="allowed")
        self.assertIsNone(session_usage(no_utilization))


class TestUsageWatcher(unittest.TestCase):
    def test_line_split_across_chunks(self) -> None:
        watcher = UsageWatcher("src")
        line = rate_event(unifiedWindows=unified(0.85))
        middle = len(line) // 2
        self.assertEqual(watcher.feed(line[:middle]), [])
        self.assertEqual(
            watcher.feed(line[middle:]), [UsageAlert("five_hour", 0.85, RESET, "src")]
        )

    def test_one_alert_per_reset(self) -> None:
        watcher = UsageWatcher("src")
        alerts = []
        for utilization in (0.8, 0.85, 0.9):
            alerts += watcher.feed(rate_event(unifiedWindows=unified(utilization)))
        self.assertEqual(alerts, [UsageAlert("five_hour", 0.8, RESET, "src")])
        new_window = watcher.feed(rate_event(unifiedWindows=unified(0.81, RESET + 18000)))
        self.assertEqual(new_window, [UsageAlert("five_hour", 0.81, RESET + 18000, "src")])

    def test_non_json_lines_ignored(self) -> None:
        self.assertEqual(UsageWatcher("src").feed("warning: foo\n"), [])

    def test_source_and_workflow_id(self) -> None:
        watcher = UsageWatcher("implement-issue-abc/implement_plan")
        [alert] = watcher.feed(rate_event(unifiedWindows=unified(0.9)))
        self.assertEqual(alert.source, "implement-issue-abc/implement_plan")
        self.assertEqual(usage_alert_workflow_id(alert), "usage-alert-five-hour-1795440000")


if __name__ == "__main__":
    unittest.main()
