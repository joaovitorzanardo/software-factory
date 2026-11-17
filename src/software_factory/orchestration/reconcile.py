"""Regras puras da reconciliação: qual etapa uma issue do Linear espera e desde quando."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from software_factory.orchestration.dispatch import KIND_IMPLEMENT, KIND_PLAN, KIND_REVIEW
from software_factory.orchestration.models import (
    STATE_IN_PROGRESS,
    STATE_IN_REVIEW,
    STATE_TODO,
    PendingIssue,
    PlanIssueInput,
)

KIND_BY_STATE: dict[str, str] = {
    STATE_TODO.lower(): KIND_PLAN,
    STATE_IN_PROGRESS.lower(): KIND_IMPLEMENT,
    STATE_IN_REVIEW.lower(): KIND_REVIEW,
}
STATE_BY_KIND = {kind: state for state, kind in KIND_BY_STATE.items()}


def _latest(timestamps: Iterable[str]) -> str | None:
    # A ordem do `history` do Linear não é garantida: compara as datas, não a posição.
    return max(timestamps, key=datetime.fromisoformat, default=None)


def trigger_time(node: dict[str, Any], kind: str, trigger_label_ids: set[str]) -> str:
    """Quando aconteceu o evento que dispara `kind`; `createdAt` se o histórico não mostra."""
    history = (node.get("history") or {}).get("nodes") or []
    if kind == KIND_PLAN:
        candidates = [node["createdAt"]] + [
            entry["createdAt"]
            for entry in history
            if trigger_label_ids & set(entry.get("addedLabelIds") or [])
        ]
    else:
        state = STATE_BY_KIND[kind]
        candidates = [
            entry["createdAt"]
            for entry in history
            if str((entry.get("toState") or {}).get("name", "")).lower() == state
        ]
    return _latest(candidates) or node["createdAt"]


def pending_issue(node: dict[str, Any], trigger_labels: Iterable[str]) -> PendingIssue | None:
    """Etapa que a issue espera pelo status atual; None sem label de gatilho ou fora dos status."""
    kind = KIND_BY_STATE.get(str((node.get("state") or {}).get("name", "")).lower())
    if kind is None:
        return None
    triggers = {name.lower() for name in trigger_labels}
    trigger_ids = {
        label["id"]
        for label in (node.get("labels") or {}).get("nodes") or []
        if label.get("name", "").lower() in triggers
    }
    if not trigger_ids:
        return None
    return PendingIssue(
        kind=kind,
        issue=PlanIssueInput(
            issue_id=node["id"],
            identifier=node.get("identifier", ""),
            title=node.get("title", ""),
            description=node.get("description"),
            url=node.get("url"),
        ),
        triggered_at=trigger_time(node, kind, trigger_ids),
    )
