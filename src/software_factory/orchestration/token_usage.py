"""Contagem de tokens de uma sessão do Claude a partir do stream-json, e a fatia da janela de uso."""

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from software_factory.orchestration.usage import _number, five_hour_window

STAGE_PLAN = "plan"
STAGE_IMPLEMENT = "implement"
STAGE_REVIEW = "review"
# Ordem de exibição.
STAGE_LABELS: dict[str, str] = {
    STAGE_PLAN: "Planejamento",
    STAGE_IMPLEMENT: "Implementação",
    STAGE_REVIEW: "Review",
}
_WORKFLOW_PREFIXES = {
    "plan-issue-": STAGE_PLAN,
    "implement-issue-": STAGE_IMPLEMENT,
    "review-issue-": STAGE_REVIEW,
}


@dataclass(frozen=True)
class TokenCounts:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def __add__(self, other: "TokenCounts") -> "TokenCounts":
        return TokenCounts(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_tokens + other.cache_creation_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class WindowSample:
    """Primeira e última utilização vista de uma janela de 5h (identificada pelo `resets_at`)."""

    resets_at: int
    first: float
    last: float


@dataclass(frozen=True)
class SessionUsage:
    tokens: TokenCounts
    cost_usd: float | None
    model: str | None
    complete: bool  # True: veio do `result` (soma da sessão); False: estimado pelos `assistant`
    windows: tuple[WindowSample, ...]


def _counts(usage: Any) -> TokenCounts | None:
    if not isinstance(usage, dict):
        return None

    def field(name: str) -> int:
        value = usage.get(name)
        return int(value) if _number(value) else 0

    return TokenCounts(
        field("input_tokens"),
        field("output_tokens"),
        field("cache_creation_input_tokens"),
        field("cache_read_input_tokens"),
    )


class TokenMeter:
    """Remonta as linhas do stdout em chunks e acumula tokens, custo e utilização da janela."""

    def __init__(self) -> None:
        self._pending = ""
        self._assistant: dict[str, TokenCounts] = {}  # último uso visto por message.id
        self._result: TokenCounts | None = None
        self._cost: float | None = None
        self._model: str | None = None
        self._windows: dict[int, WindowSample] = {}

    def feed(self, chunk: str) -> None:
        *lines, self._pending = (self._pending + chunk).split("\n")
        for line in lines:
            self._parse(line)

    def _parse(self, line: str) -> bool:
        """False se a linha não é JSON válido (pode estar incompleta)."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        if isinstance(event, dict):
            self._handle(event)
        return True

    def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            model = event.get("model")
            self._model = model if isinstance(model, str) else self._model
        elif kind == "assistant":
            message = event.get("message")
            counts = _counts(message.get("usage")) if isinstance(message, dict) else None
            message_id = message.get("id") if isinstance(message, dict) else None
            if counts is not None and isinstance(message_id, str):
                self._assistant[message_id] = counts
        elif kind == "result":
            self._result = _counts(event.get("usage")) or self._result
            cost = event.get("total_cost_usd")
            self._cost = float(cost) if _number(cost) else self._cost
        elif kind == "rate_limit_event":
            window = five_hour_window(event)
            if window is not None:
                utilization, resets_at = window
                seen = self._windows.get(resets_at)
                first = seen.first if seen else utilization
                self._windows[resets_at] = WindowSample(resets_at, first, utilization)

    def usage(self) -> SessionUsage | None:
        # A última linha (o `result`) pode chegar sem "\n": se já é um JSON completo, processa
        # uma vez e limpa o buffer; se ainda está incompleta, segue no buffer.
        if self._pending and self._parse(self._pending):
            self._pending = ""
        if self._result is None and not self._assistant and not self._windows:
            return None
        if self._result is not None:
            tokens, complete = self._result, True
        else:
            tokens, complete = sum(self._assistant.values(), TokenCounts()), False
        return SessionUsage(
            tokens=tokens,
            cost_usd=self._cost,
            model=self._model,
            complete=complete,
            windows=tuple(self._windows.values()),
        )


def session_share(samples: Iterable[WindowSample]) -> float | None:
    """Fatia da janela de 5h consumida: por janela, maior `last` menos menor `first`; soma das janelas."""
    spans: dict[int, tuple[float, float]] = {}
    for sample in samples:
        low, high = spans.get(sample.resets_at, (sample.first, sample.last))
        spans[sample.resets_at] = (min(low, sample.first), max(high, sample.last))
    if not spans:
        return None
    return sum(high - low for low, high in spans.values())


def stage_for_workflow(workflow_id: str) -> tuple[str, str] | None:
    """`(etapa, issue_id)` de um id de workflow de issue; None para os demais."""
    for prefix, stage in _WORKFLOW_PREFIXES.items():
        if workflow_id.startswith(prefix):
            return stage, workflow_id[len(prefix) :]
    return None


def token_usage_comment_id(issue_id: str) -> str:
    """UUID v4 determinístico do comentário de tokens da issue (o Linear exige v4)."""
    digest = hashlib.sha256(f"token-usage:{issue_id}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


@dataclass(frozen=True)
class StageUsage:
    tokens: TokenCounts
    cost_usd: float | None  # soma dos custos conhecidos; None se nenhuma sessão tem custo
    share: float | None  # fatia da janela de 5h (0 a 1); None sem amostras
    sessions: int
    partial: bool  # alguma sessão sem `result` (contagem estimada)


@dataclass(frozen=True)
class IssueUsage:
    stages: dict[str, StageUsage]  # só etapas com sessão, na ordem de STAGE_LABELS
    total: StageUsage | None  # None sem sessões


def _int(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _usd(value: float) -> str:
    return "US$ " + f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _summary_line(label: str, stage: StageUsage) -> str:
    line = f"{label}: {_int(stage.tokens.total)} tokens"
    if stage.partial:
        line += " (parcial)"
    if stage.share is not None:
        line += f" · {f'{stage.share * 100:.1f}'.replace('.', ',')}% da sessão"
    if stage.cost_usd is not None:
        line += f" · {_usd(stage.cost_usd)}"
    return line


def _table_row(label: str, stage: StageUsage) -> str:
    t = stage.tokens
    cost = _usd(stage.cost_usd) if stage.cost_usd is not None else "—"
    cells = [
        label,
        str(stage.sessions),
        _int(t.input_tokens),
        _int(t.cache_creation_tokens),
        _int(t.cache_read_tokens),
        _int(t.output_tokens),
        cost,
    ]
    return "| " + " | ".join(cells) + " |"


def render_usage_comment(usage: IssueUsage, final: bool) -> str:
    """Texto do comentário de tokens no Linear; a linha `Total` só com `final`."""
    title = "**Tokens gastos pela factory**"
    if not usage.stages:
        return f"{title}\n\nNenhuma sessão do Claude registrada."
    rows = [(STAGE_LABELS[name], stage) for name, stage in usage.stages.items()]
    if final and usage.total is not None:
        rows.append(("Total", usage.total))
    header = [
        "| Etapa | Sessões | Input | Cache (escrita) | Cache (leitura) | Output | Custo |",
        "|---|---|---|---|---|---|---|",
    ]
    lines = [title, ""]
    lines += [_summary_line(label, stage) for label, stage in rows]
    lines += ["", "+++ Detalhes", ""] + header
    lines += [_table_row(label, stage) for label, stage in rows]
    lines += ["", "+++"]
    return "\n".join(lines)
