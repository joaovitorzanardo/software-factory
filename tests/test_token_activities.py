import tempfile
import unittest
import uuid
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.models import CommentRequest, UsageReportRequest
from software_factory.orchestration.token_activities import TokenUsageActivities
from software_factory.orchestration.token_reporting import publish_token_usage
from software_factory.orchestration.token_store import SessionRecord, TokenStore
from software_factory.orchestration.token_usage import (
    SessionUsage,
    TokenCounts,
    token_usage_comment_id,
)


@workflow.defn
class PublishWorkflow:
    @workflow.run
    async def run(self, issue_id: str) -> None:
        await publish_token_usage(issue_id)


def plan_record(issue_id: str) -> SessionRecord:
    return SessionRecord(
        "k1", issue_id, "plan", f"plan-issue-{issue_id}", "run-1", "generate_plan", 1,
        "2026-11-18T10:00:00Z", SessionUsage(TokenCounts(10, 1, 2, 3), 0.5, "claude-x", True, ()),
    )  # fmt: skip


class TestTokenUsageReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TokenStore(str(Path(self._tmp.name) / "t.db"))
        self.store.record(plan_record("i1"))
        self.activities = TokenUsageActivities(self.store)

    async def test_report_renders_from_store(self) -> None:
        body = await self.activities.token_usage_report(UsageReportRequest("i1"))
        self.assertIn("Planejamento:", body)

    async def test_report_final_has_total(self) -> None:
        body = await self.activities.token_usage_report(UsageReportRequest("i1", final=True))
        self.assertIn("Total", body)
        partial = await self.activities.token_usage_report(UsageReportRequest("i1"))
        self.assertNotIn("Total", partial)


class TestPublishTokenUsage(unittest.IsolatedAsyncioTestCase):
    async def run_publish(self, upsert) -> None:
        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[PublishWorkflow],
                activities=[self.report, upsert],
            ):
                await env.client.execute_workflow(
                    PublishWorkflow.run, "i1", id=f"w-{uuid.uuid4()}", task_queue=task_queue
                )

    @activity.defn(name="token_usage_report")
    async def report(self, request: UsageReportRequest) -> str:
        return f"corpo {request.issue_id} final={request.final}"

    async def test_publish_calls_report_then_upsert(self) -> None:
        received: list[CommentRequest] = []

        @activity.defn(name="upsert_comment")
        async def upsert(request: CommentRequest) -> str:
            received.append(request)
            return request.comment_id

        await self.run_publish(upsert)
        self.assertEqual(
            received,
            [CommentRequest("i1", token_usage_comment_id("i1"), "corpo i1 final=False")],
        )

    async def test_publish_swallows_activity_failure(self) -> None:
        @activity.defn(name="upsert_comment")
        async def upsert(request: CommentRequest) -> str:
            raise ApplicationError("Linear fora", non_retryable=True)

        await self.run_publish(upsert)
