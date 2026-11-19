import copy
import json
import unittest
import uuid

from software_factory.orchestration.token_usage import (
    STAGE_IMPLEMENT,
    STAGE_PLAN,
    IssueUsage,
    SessionUsage,
    StageUsage,
    TokenCounts,
    TokenMeter,
    WindowSample,
    render_usage_comment,
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


def stage(total: int, share: float | None, cost: float | None, partial: bool = False) -> StageUsage:
    return StageUsage(TokenCounts(input_tokens=total), cost, share, 1, partial)


class TestRenderUsageComment(unittest.TestCase):
    def usage(self) -> IssueUsage:
        plan = StageUsage(TokenCounts(45_000, 18_000, 60_000, 1_111_567), 0.84, 0.032, 1, False)
        impl = StageUsage(TokenCounts(4_800_000), 3.10, 0.14, 2, True)
        total = StageUsage(plan.tokens + impl.tokens, 3.94, 0.172, 3, True)
        return IssueUsage({STAGE_PLAN: plan, STAGE_IMPLEMENT: impl}, total)

    def test_render_stages_in_order(self) -> None:
        text = render_usage_comment(self.usage(), final=False)
        plan_line = "Planejamento: 1.234.567 tokens · 3,2% da sessão · US$ 0,84"
        self.assertIn(plan_line, text)
        self.assertIn("Implementação: 4.800.000 tokens (parcial) · 14,0% da sessão · US$ 3,10", text)
        self.assertLess(text.index(plan_line), text.index("Implementação:"))
        self.assertNotIn("Total", text)
        self.assertIn("| Planejamento | 1 | 45.000 | 60.000 | 1.111.567 | 18.000 | US$ 0,84 |", text)

    def test_render_final_adds_total(self) -> None:
        text = render_usage_comment(self.usage(), final=True)
        self.assertIn("Total: 6.034.567 tokens (parcial) · 17,2% da sessão · US$ 3,94", text)
        self.assertIn("| Total | 3 |", text)

    def test_render_without_share_or_cost(self) -> None:
        usage = IssueUsage({STAGE_PLAN: stage(1000, None, None)}, None)
        text = render_usage_comment(usage, final=False)
        self.assertIn("Planejamento: 1.000 tokens\n", text)
        self.assertNotIn("·", text)
        self.assertIn("| — |", text)

    def test_render_partial(self) -> None:
        usage = IssueUsage({STAGE_PLAN: stage(5, None, None, partial=True)}, None)
        self.assertIn("Planejamento: 5 tokens (parcial)", render_usage_comment(usage, final=False))

    def test_render_empty(self) -> None:
        self.assertEqual(
            render_usage_comment(IssueUsage({}, None), final=True),
            "**Tokens gastos pela factory**\n\nNenhuma sessão do Claude registrada.",
        )
