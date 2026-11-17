import unittest

from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.orchestration.dispatch import (
    KIND_IMPLEMENT,
    KIND_PLAN,
    KIND_REVIEW,
    start_issue_workflow,
)
from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import PlanIssueInput
from software_factory.orchestration.review_workflow import ReviewIssueWorkflow
from software_factory.orchestration.workflow import PlanIssueWorkflow

ISSUE = PlanIssueInput(
    issue_id="issue-1", identifier="ENG-12", title="Login", description=None, url=None
)


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[tuple[tuple, dict]] = []

    async def start_workflow(self, *args, **kwargs) -> None:
        self.started.append((args, kwargs))
        if self.error:
            raise self.error


class TestStartIssueWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_starts_each_kind(self) -> None:
        cases = [
            (KIND_PLAN, PlanIssueWorkflow.run, "plan-issue"),
            (KIND_IMPLEMENT, ImplementIssueWorkflow.run, "implement-issue"),
            (KIND_REVIEW, ReviewIssueWorkflow.run, "review-issue"),
        ]
        for kind, run, prefix in cases:
            with self.subTest(kind=kind):
                client = FakeClient()
                self.assertTrue(await start_issue_workflow(client, "q", kind, ISSUE))
                ((args, kwargs),) = client.started
                self.assertIs(args[0], run)
                self.assertIs(args[1], ISSUE)
                self.assertEqual(kwargs["id"], f"{prefix}-issue-1")
                self.assertEqual(kwargs["task_queue"], "q")

    async def test_already_started_returns_false(self) -> None:
        client = FakeClient(WorkflowAlreadyStartedError("plan-issue-issue-1", "PlanIssueWorkflow"))
        self.assertFalse(await start_issue_workflow(client, "q", KIND_PLAN, ISSUE))

    async def test_other_errors_propagate(self) -> None:
        client = FakeClient(RuntimeError("temporal fora"))
        with self.assertRaises(RuntimeError):
            await start_issue_workflow(client, "q", KIND_PLAN, ISSUE)
