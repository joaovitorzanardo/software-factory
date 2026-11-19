"""Persistência em SQLite do uso de tokens por sessão do Claude, agregado por issue e etapa."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from software_factory.orchestration.token_usage import (
    STAGE_LABELS,
    IssueUsage,
    SessionUsage,
    StageUsage,
    TokenCounts,
    WindowSample,
    session_share,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    key TEXT PRIMARY KEY, issue_id TEXT NOT NULL, stage TEXT NOT NULL,
    workflow_id TEXT, run_id TEXT, activity TEXT, attempt INTEGER, recorded_at TEXT,
    model TEXT, complete INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cache_creation_tokens INTEGER, cache_read_tokens INTEGER, cost_usd REAL);
CREATE TABLE IF NOT EXISTS session_windows(
    session_key TEXT, resets_at INTEGER, util_first REAL, util_last REAL,
    PRIMARY KEY (session_key, resets_at));
CREATE INDEX IF NOT EXISTS sessions_issue ON sessions(issue_id);
"""


@dataclass(frozen=True)
class SessionRecord:
    key: str
    issue_id: str
    stage: str
    workflow_id: str
    run_id: str
    activity: str
    attempt: int
    recorded_at: str
    usage: SessionUsage


@dataclass
class _Row:
    tokens: TokenCounts
    cost_usd: float | None
    complete: bool
    windows: list[WindowSample]


def _aggregate(rows: list[_Row]) -> StageUsage:
    tokens = sum((row.tokens for row in rows), TokenCounts())
    costs = [row.cost_usd for row in rows if row.cost_usd is not None]
    samples = [window for row in rows for window in row.windows]
    return StageUsage(
        tokens=tokens,
        cost_usd=sum(costs) if costs else None,
        share=session_share(samples),
        sessions=len(rows),
        partial=any(not row.complete for row in rows),
    )


class TokenStore:
    """Uma linha por sessão; chamadas síncronas (use `asyncio.to_thread` nas activities)."""

    def __init__(self, path: str) -> None:
        self._path = path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(_SCHEMA)
            # Gancho para migrações futuras; PRAGMA não aceita parâmetros.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record(self, record: SessionRecord) -> None:
        """Grava a sessão; a mesma chave de novo é ignorada (retry da activity)."""
        usage = record.usage
        with closing(sqlite3.connect(self._path)) as conn, conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO sessions(key, issue_id, stage, workflow_id, run_id, activity,"
                " attempt, recorded_at, model, complete, input_tokens, output_tokens,"
                " cache_creation_tokens, cache_read_tokens, cost_usd)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.key, record.issue_id, record.stage, record.workflow_id,
                    record.run_id, record.activity, record.attempt, record.recorded_at,
                    usage.model, int(usage.complete), usage.tokens.input_tokens,
                    usage.tokens.output_tokens, usage.tokens.cache_creation_tokens,
                    usage.tokens.cache_read_tokens, usage.cost_usd,
                ),
            ).rowcount  # fmt: skip
            if inserted:
                conn.executemany(
                    "INSERT OR IGNORE INTO session_windows VALUES (?,?,?,?)",
                    [(record.key, w.resets_at, w.first, w.last) for w in usage.windows],
                )

    def issue_usage(self, issue_id: str) -> IssueUsage:
        with closing(sqlite3.connect(self._path)) as conn:
            sessions = conn.execute(
                "SELECT key, stage, complete, input_tokens, output_tokens,"
                " cache_creation_tokens, cache_read_tokens, cost_usd"
                " FROM sessions WHERE issue_id = ?",
                (issue_id,),
            ).fetchall()
            windows = conn.execute(
                "SELECT w.session_key, w.resets_at, w.util_first, w.util_last"
                " FROM session_windows w JOIN sessions s ON s.key = w.session_key"
                " WHERE s.issue_id = ?",
                (issue_id,),
            ).fetchall()
        by_key: dict[str, list[WindowSample]] = {}
        for key, resets_at, first, last in windows:
            by_key.setdefault(key, []).append(WindowSample(resets_at, first, last))
        by_stage: dict[str, list[_Row]] = {}
        for key, stage, complete, inp, out, cache_creation, cache_read, cost in sessions:
            row = _Row(
                TokenCounts(inp, out, cache_creation, cache_read),
                cost,
                bool(complete),
                by_key.get(key, []),
            )
            by_stage.setdefault(stage, []).append(row)
        stages = {
            stage: _aggregate(by_stage[stage]) for stage in STAGE_LABELS if stage in by_stage
        }
        everything = [row for rows in by_stage.values() for row in rows]
        return IssueUsage(stages, _aggregate(everything) if everything else None)
