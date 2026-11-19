import copy
import json
import unittest
import uuid

from software_factory.orchestration.token_usage import (
    STAGE_IMPLEMENT,
    SessionUsage,
    TokenCounts,
    TokenMeter,
    WindowSample,
    session_share,
    stage_for_workflow,
    token_usage_comment_id,
)
from tests.stream_samples import ASSISTANT_EVENT, INIT_EVENT, RATE_LIMIT_EVENT, RESULT_EVENT


def stream(*events: object) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def assistant(msg_id: str, output_tokens: int) -> dict:
    event = copy.deepcopy(ASSISTANT_EVENT)
    event["message"]["id"] = msg_id
    event["message"]["usage"]["output_tokens"] = output_tokens
    return event


def rate_limit(utilization: float, resets_at: int) -> dict:
    event = copy.deepcopy(RATE_LIMIT_EVENT)
    event["rate_limit_info"]["unifiedWindows"]["five_hour"] = {
        "utilization": utilization,
        "resetsAt": resets_at,
    }
    return event


class TestTokenMeter(unittest.TestCase):
    def test_uses_result_usage(self) -> None:
        meter = TokenMeter()
        meter.feed(stream(INIT_EVENT, ASSISTANT_EVENT, RESULT_EVENT))
        usage = meter.usage()
        self.assertIsInstance(usage, SessionUsage)
        self.assertEqual(usage.tokens, TokenCounts(4, 175, 40261, 37118))
        self.assertTrue(usage.complete)
        self.assertEqual(usage.cost_usd, RESULT_EVENT["total_cost_usd"])
        self.assertEqual(usage.model, "claude-opus-5-5")

    def test_total_and_add(self) -> None:
        total = TokenCounts(1, 2, 3, 4) + TokenCounts(1, 1, 1, 1)
        self.assertEqual(total, TokenCounts(2, 3, 4, 5))
        self.assertEqual(total.total, 14)

    def test_falls_back_to_assistant_usage(self) -> None:
        meter = TokenMeter()
        meter.feed(stream(assistant("a", 10), assistant("b", 20)))
        usage = meter.usage()
        self.assertEqual(usage.tokens, TokenCounts(4, 30, 6286, 74236))
        self.assertFalse(usage.complete)
        self.assertIsNone(usage.cost_usd)

    def test_dedupes_assistant_by_message_id(self) -> None:
        meter = TokenMeter()
        meter.feed(stream(assistant("a", 4), assistant("a", 90)))
        self.assertEqual(meter.usage().tokens, TokenCounts(2, 90, 3143, 37118))

    def test_line_split_across_chunks(self) -> None:
        text = stream(RESULT_EVENT)
        meter = TokenMeter()
        meter.feed(text[:40])
        self.assertIsNone(meter.usage())
        meter.feed(text[40:])
        self.assertEqual(meter.usage().tokens.output_tokens, 175)

    def test_tracks_first_and_last_utilization_per_window(self) -> None:
        meter = TokenMeter()
        meter.feed(stream(rate_limit(0.30, 100), rate_limit(0.35, 100), rate_limit(0.02, 200)))
        self.assertEqual(
            meter.usage().windows, (WindowSample(100, 0.30, 0.35), WindowSample(200, 0.02, 0.02))
        )

    def test_ignores_garbage_lines(self) -> None:
        meter = TokenMeter()
        meter.feed("não é json\n[1, 2]\n42\n")
        self.assertIsNone(meter.usage())

    def test_usage_none_without_data(self) -> None:
        self.assertIsNone(TokenMeter().usage())


class TestSessionShare(unittest.TestCase):
    def test_overlapping_sessions(self) -> None:
        share = session_share([WindowSample(1, 0.30, 0.40), WindowSample(1, 0.32, 0.45)])
        self.assertAlmostEqual(share, 0.15)

    def test_across_windows(self) -> None:
        share = session_share([WindowSample(1, 0.90, 0.98), WindowSample(2, 0.00, 0.05)])
        self.assertAlmostEqual(share, 0.13)

    def test_none_without_samples(self) -> None:
        self.assertIsNone(session_share([]))


class TestIds(unittest.TestCase):
    def test_stage_for_workflow(self) -> None:
        self.assertEqual(stage_for_workflow("implement-issue-abc"), (STAGE_IMPLEMENT, "abc"))
        self.assertIsNone(stage_for_workflow("usage-alert-five-hour-1"))

    def test_comment_id_is_stable(self) -> None:
        self.assertEqual(token_usage_comment_id("a"), token_usage_comment_id("a"))
        self.assertNotEqual(token_usage_comment_id("a"), token_usage_comment_id("b"))
        self.assertEqual(uuid.UUID(token_usage_comment_id("a")).version, 4)
