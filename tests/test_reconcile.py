import unittest
from typing import Any

from software_factory.orchestration.reconcile import pending_issue
from tests.payloads import BUG, DOCS, FEATURE

TRIGGERS = ("Feature", "Bug")
CREATED = "2026-11-14T09:00:00.000Z"


def node(
    state: str,
    labels: list[dict[str, str]],
    created_at: str = CREATED,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "issue-1",
        "identifier": "ENG-12",
        "title": "Login",
        "description": "Descrição",
        "url": "https://linear.app/acme/issue/ENG-12",
        "createdAt": created_at,
        "state": {"name": state},
        "labels": {"nodes": labels},
        "history": {"nodes": history or []},
    }


def entry(created_at: str, to_state: str | None = None, added: list[str] | None = None):
    return {
        "createdAt": created_at,
        "toState": {"name": to_state} if to_state else None,
        "addedLabelIds": added,
    }


class TestPendingIssue(unittest.TestCase):
    def test_todo_with_label_is_plan(self) -> None:
        pending = pending_issue(node("To Do", [FEATURE]), TRIGGERS)
        self.assertEqual(pending.kind, "plan")
        self.assertEqual(pending.issue.issue_id, "issue-1")
        self.assertEqual(pending.issue.identifier, "ENG-12")
        self.assertEqual(pending.issue.url, "https://linear.app/acme/issue/ENG-12")
        self.assertEqual(pending.triggered_at, CREATED)

    def test_label_case_insensitive(self) -> None:
        self.assertIsNotNone(pending_issue(node("to do", [BUG]), TRIGGERS))

    def test_no_trigger_label_is_none(self) -> None:
        self.assertIsNone(pending_issue(node("To Do", [DOCS]), TRIGGERS))

    def test_in_planning_is_none(self) -> None:
        self.assertIsNone(pending_issue(node("In Planning", [FEATURE]), TRIGGERS))

    def test_plan_trigger_is_latest_label_add(self) -> None:
        history = [
            entry("2026-11-15T10:00:00.000Z", added=["label-feature"]),
            entry("2026-11-15T12:00:00.000Z", added=["label-docs"]),
        ]
        pending = pending_issue(node("To Do", [FEATURE, DOCS], history=history), TRIGGERS)
        self.assertEqual(pending.triggered_at, "2026-11-15T10:00:00.000Z")

    def test_in_progress_trigger_is_latest_entry_into_state(self) -> None:
        history = [
            entry("2026-11-15T10:00:00.000Z", to_state="In Progress"),
            entry("2026-11-15T12:00:00.000Z", to_state="In Progress"),
            entry("2026-11-15T11:00:00.000Z", to_state="In Review"),
        ]
        pending = pending_issue(node("In Progress", [FEATURE], history=history), TRIGGERS)
        self.assertEqual(pending.kind, "implement")
        self.assertEqual(pending.triggered_at, "2026-11-15T12:00:00.000Z")

    def test_in_review_without_history_uses_created_at(self) -> None:
        pending = pending_issue(node("In Review", [FEATURE]), TRIGGERS)
        self.assertEqual(pending.kind, "review")
        self.assertEqual(pending.triggered_at, CREATED)
