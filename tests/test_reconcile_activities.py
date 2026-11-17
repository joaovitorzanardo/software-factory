import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from google.protobuf.duration_pb2 import Duration
from temporalio.api.workflowservice.v1 import DescribeNamespaceResponse
from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.linear.client import LinearClient
from software_factory.orchestration.models import PendingIssue, PlanIssueInput, ReconcileInput
from software_factory.orchestration.reconcile_activities import ReconcileActivities
from tests.payloads import BUG, DOCS, FEATURE
from tests.test_reconcile import node

PENDING = PendingIssue(
    kind="implement",
    issue=PlanIssueInput(
        issue_id="issue-1", identifier="ENG-12", title="Login", description=None, url=None
    ),
    triggered_at="2026-11-16T10:00:00.000Z",
)


class FakeTemporal:
    def __init__(
        self,
        count: int = 0,
        start_error: Exception | None = None,
        retention: timedelta = timedelta(days=3650),
    ) -> None:
        self.count = count
        self.start_error = start_error
        self.queries: list[str] = []
        self.started: list[tuple[tuple, dict]] = []
        self.namespace = "default"
        self.described: list[str] = []
        response = DescribeNamespaceResponse()
        response.config.workflow_execution_retention_ttl.CopyFrom(
            Duration(seconds=int(retention.total_seconds()))
        )

        async def describe_namespace(request):
            self.described.append(request.namespace)
            return response

        self.workflow_service = SimpleNamespace(describe_namespace=describe_namespace)

    async def count_workflows(self, query: str) -> SimpleNamespace:
        self.queries.append(query)
        return SimpleNamespace(count=self.count)

    async def start_workflow(self, *args, **kwargs) -> None:
        self.started.append((args, kwargs))
        if self.start_error:
            raise self.start_error


def linear_pages(pages: list[dict], requests: list[httpx.Request]) -> LinearClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"issues": pages[len(requests) - 1]}})

    return LinearClient("lin-key", transport=httpx.MockTransport(handler))


def activities(temporal: FakeTemporal, linear: LinearClient | None = None) -> ReconcileActivities:
    return ReconcileActivities(linear or LinearClient("k"), temporal, "q")


class TestListPendingIssues(unittest.IsolatedAsyncioTestCase):
    async def test_lists_all_pages(self) -> None:
        review = dict(node("In Review", [BUG]), id="issue-2")
        unlabeled = dict(node("To Do", [DOCS]), id="issue-3")
        pages = [
            {"nodes": [node("To Do", [FEATURE])], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
            {"nodes": [review, unlabeled], "pageInfo": {"hasNextPage": False, "endCursor": "c2"}},
        ]
        requests: list[httpx.Request] = []
        result = await activities(FakeTemporal(), linear_pages(pages, requests)).list_pending_issues(
            ReconcileInput(trigger_labels=("Feature", "Bug"))
        )
        self.assertEqual([(p.kind, p.issue.issue_id) for p in result], [("plan", "issue-1"), ("review", "issue-2")])
        self.assertEqual(len(requests), 2)
        self.assertIsNone(json.loads(requests[0].content)["variables"]["after"])
        self.assertEqual(json.loads(requests[1].content)["variables"]["after"], "c1")


class TestStartMissedWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_run_after_trigger(self) -> None:
        temporal = FakeTemporal(count=1)
        self.assertFalse(await activities(temporal).start_missed_workflow(PENDING))
        self.assertEqual(temporal.started, [])

    async def test_query_uses_skew_tolerance(self) -> None:
        temporal = FakeTemporal(count=1)
        await activities(temporal).start_missed_workflow(PENDING)
        self.assertEqual(
            temporal.queries,
            ["WorkflowId = 'implement-issue-issue-1' AND StartTime >= '2026-11-16T09:58:00Z'"],
        )

    async def test_starts_when_no_run(self) -> None:
        temporal = FakeTemporal(count=0)
        self.assertTrue(await activities(temporal).start_missed_workflow(PENDING))
        ((args, kwargs),) = temporal.started
        self.assertIs(args[1], PENDING.issue)
        self.assertEqual(kwargs["id"], "implement-issue-issue-1")
        self.assertEqual(kwargs["task_queue"], "q")

    async def test_running_workflow_is_skipped(self) -> None:
        temporal = FakeTemporal(
            count=0, start_error=WorkflowAlreadyStartedError("implement-issue-issue-1", "x")
        )
        self.assertFalse(await activities(temporal).start_missed_workflow(PENDING))

    async def test_skips_trigger_older_than_retention(self) -> None:
        # Runs fechados há mais que a retenção somem da visibility: sem como saber se já rodou.
        temporal = FakeTemporal(count=0, retention=timedelta(days=1))
        old = datetime.now(UTC) - timedelta(days=2)
        stale = PendingIssue(
            kind=PENDING.kind,
            issue=PENDING.issue,
            triggered_at=old.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        )
        self.assertFalse(await activities(temporal).start_missed_workflow(stale))
        self.assertEqual(temporal.described, ["default"])
        self.assertEqual(temporal.queries, [])
        self.assertEqual(temporal.started, [])

    async def test_recent_trigger_within_retention_starts(self) -> None:
        temporal = FakeTemporal(count=0, retention=timedelta(days=1))
        recent = datetime.now(UTC) - timedelta(hours=1)
        fresh = PendingIssue(
            kind=PENDING.kind,
            issue=PENDING.issue,
            triggered_at=recent.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        )
        self.assertTrue(await activities(temporal).start_missed_workflow(fresh))
