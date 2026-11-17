import unittest
import uuid

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.models import (
    PendingIssue,
    PlanIssueInput,
    ReconcileInput,
    ReconcileResult,
)
from software_factory.orchestration.reconcile_workflow import ReconcileIssuesWorkflow


def pending(kind: str, issue_id: str) -> PendingIssue:
    return PendingIssue(
        kind=kind,
        issue=PlanIssueInput(
            issue_id=issue_id, identifier=issue_id.upper(), title="t", description=None, url=None
        ),
        triggered_at="2026-11-16T10:00:00.000Z",
    )


class FakeActivities:
    def __init__(self, pendings: list[PendingIssue], starts: list[bool]) -> None:
        self.pendings = pendings
        self.starts = list(starts)
        self.list_inputs: list[ReconcileInput] = []
        self.start_inputs: list[str] = []

    @activity.defn(name="list_pending_issues")
    async def list_pending_issues(self, request: ReconcileInput) -> list[PendingIssue]:
        self.list_inputs.append(request)
        return self.pendings

    @activity.defn(name="start_missed_workflow")
    async def start_missed_workflow(self, item: PendingIssue) -> bool:
        self.start_inputs.append(item.issue.issue_id)
        return self.starts.pop(0)


class TestReconcileIssuesWorkflow(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, fake: FakeActivities) -> ReconcileResult:
        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ReconcileIssuesWorkflow],
                activities=[fake.list_pending_issues, fake.start_missed_workflow],
            ):
                return await env.client.execute_workflow(
                    ReconcileIssuesWorkflow.run,
                    ReconcileInput(trigger_labels=("bug", "feature")),
                    id=f"reconcile-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    async def test_starts_missed_and_counts_skipped(self) -> None:
        fake = FakeActivities(
            [pending("plan", "a"), pending("implement", "b"), pending("review", "c")],
            [True, False, True],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReconcileResult(started=["plan-issue-a", "review-issue-c"], skipped=1))
        self.assertEqual(fake.start_inputs, ["a", "b", "c"])
        self.assertEqual(fake.list_inputs, [ReconcileInput(trigger_labels=("bug", "feature"))])

    async def test_no_pending(self) -> None:
        result = await self.run_workflow(FakeActivities([], []))
        self.assertEqual(result, ReconcileResult(started=[], skipped=0))
