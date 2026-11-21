import sqlite3
import tempfile
import unittest
from pathlib import Path

from software_factory.orchestration.token_store import SCHEMA_VERSION, SessionRecord, TokenStore
from software_factory.orchestration.token_usage import (
    IssueUsage,
    SessionUsage,
    TokenCounts,
    WindowSample,
    session_share,
)


def usage(
    tokens: int = 10,
    cost: float | None = None,
    complete: bool = True,
    windows: tuple[WindowSample, ...] = (),
) -> SessionUsage:
    return SessionUsage(TokenCounts(tokens, 1, 2, 3), cost, "claude-x", complete, windows)


def record(key: str, stage: str, session: SessionUsage, issue_id: str = "i1") -> SessionRecord:
    return SessionRecord(
        key, issue_id, stage, f"{stage}-issue-{issue_id}", f"run-{key}", "act", 1,
        "2026-11-18T10:00:00Z", session,
    )  # fmt: skip


class TestTokenStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "t.db")
        self.store = TokenStore(self.path)

    def test_new_database_has_schema_version(self) -> None:
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_accumulates_sessions_per_stage(self) -> None:
        self.store.record(record("a", "implement", usage(10)))
        self.store.record(record("b", "implement", usage(20)))
        self.store.record(record("c", "plan", usage(5)))
        result = self.store.issue_usage("i1")
        self.assertEqual(list(result.stages), ["plan", "implement"])
        implement = result.stages["implement"]
        self.assertEqual(implement.tokens, TokenCounts(30, 2, 4, 6))
        self.assertEqual(implement.sessions, 2)
        assert result.total is not None
        self.assertEqual(result.total.sessions, 3)
        self.assertEqual(result.total.tokens.input_tokens, 35)

    def test_rerun_accumulates(self) -> None:
        self.store.record(record("a", "plan", usage(10)))
        self.store.record(record("b", "plan", usage(7)))
        self.assertEqual(self.store.issue_usage("i1").stages["plan"].tokens.input_tokens, 17)

    def test_cost_none_when_no_session_has_cost(self) -> None:
        self.store.record(record("a", "plan", usage()))
        result = self.store.issue_usage("i1")
        self.assertIsNone(result.stages["plan"].cost_usd)
        assert result.total is not None
        self.assertIsNone(result.total.cost_usd)

    def test_cost_sums_known_costs(self) -> None:
        self.store.record(record("a", "plan", usage(cost=0.5)))
        self.store.record(record("b", "plan", usage(cost=0.25)))
        self.store.record(record("c", "plan", usage()))
        self.assertAlmostEqual(self.store.issue_usage("i1").stages["plan"].cost_usd or 0, 0.75)

    def test_partial_flag(self) -> None:
        self.store.record(record("a", "plan", usage()))
        self.store.record(record("b", "implement", usage(complete=False)))
        result = self.store.issue_usage("i1")
        self.assertFalse(result.stages["plan"].partial)
        self.assertTrue(result.stages["implement"].partial)
        assert result.total is not None
        self.assertTrue(result.total.partial)

    def test_share_from_windows(self) -> None:
        w1 = WindowSample(100, 0.10, 0.20)
        w2 = WindowSample(100, 0.20, 0.35)
        w3 = WindowSample(200, 0.0, 0.05)
        self.store.record(record("a", "implement", usage(windows=(w1,))))
        self.store.record(record("b", "implement", usage(windows=(w2,))))
        self.store.record(record("c", "plan", usage(windows=(w3,))))
        self.store.record(record("d", "review", usage()))
        result = self.store.issue_usage("i1")
        self.assertAlmostEqual(
            result.stages["implement"].share or 0, session_share([w1, w2]) or 0
        )
        self.assertIsNone(result.stages["review"].share)
        assert result.total is not None
        self.assertAlmostEqual(result.total.share or 0, session_share([w1, w2, w3]) or 0)

    def test_record_is_idempotent_by_key(self) -> None:
        w = WindowSample(100, 0.1, 0.2)
        self.store.record(record("a", "plan", usage(windows=(w,))))
        self.store.record(record("a", "plan", usage(windows=(w,))))
        result = self.store.issue_usage("i1")
        self.assertEqual(result.stages["plan"].sessions, 1)
        self.assertEqual(result.stages["plan"].tokens.input_tokens, 10)

    def test_empty_issue(self) -> None:
        self.assertEqual(self.store.issue_usage("nope"), IssueUsage(stages={}, total=None))

    def test_other_issue_not_mixed(self) -> None:
        self.store.record(record("a", "plan", usage(), issue_id="i1"))
        self.store.record(record("b", "plan", usage(), issue_id="i2"))
        assert self.store.issue_usage("i1").total is not None
        self.assertEqual(self.store.issue_usage("i1").total.sessions, 1)

    def test_persists_across_instances(self) -> None:
        self.store.record(record("a", "plan", usage(42)))
        other = TokenStore(self.path)
        self.assertEqual(other.issue_usage("i1").stages["plan"].tokens.input_tokens, 42)


if __name__ == "__main__":
    unittest.main()
