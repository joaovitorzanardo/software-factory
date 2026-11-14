import json
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode
from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.api import create_app
from software_factory.config import Settings
from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import HumanAnswer
from software_factory.orchestration.review_workflow import ReviewIssueWorkflow
from software_factory.orchestration.workflow import PlanIssueWorkflow
from tests.payloads import DOCS, FEATURE, comment_payload, issue_payload
from tests.test_signature import SECRET, sign


def now_ms() -> int:
    return int(time.time() * 1000)


class FakeTemporalClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[tuple[tuple, dict]] = []
        self.signals: list[tuple[str, object, object]] = []
        self.signal_error: Exception | None = None
        self.signal_errors: dict[str, Exception] = {}

    def get_workflow_handle(self, workflow_id: str) -> "FakeHandle":
        return FakeHandle(self, workflow_id)

    async def start_workflow(self, *args, **kwargs) -> None:
        self.started.append((args, kwargs))
        if self.error:
            raise self.error


class FakeHandle:
    def __init__(self, client: FakeTemporalClient, workflow_id: str) -> None:
        self.client = client
        self.workflow_id = workflow_id

    async def signal(self, signal, arg) -> None:
        if self.client.signal_error:
            raise self.client.signal_error
        if self.workflow_id in self.client.signal_errors:
            raise self.client.signal_errors[self.workflow_id]
        self.client.signals.append((self.workflow_id, signal, arg))


class TestLinearWebhook(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = FakeTemporalClient()
        self.client = self.make_client(self.temporal)

    def make_client(self, temporal: FakeTemporalClient) -> TestClient:
        return TestClient(
            create_app(Settings(linear_webhook_secret=SECRET), temporal_client=temporal)
        )

    def accepted_body(self) -> bytes:
        return json.dumps(issue_payload(labels=[FEATURE], webhook_timestamp=now_ms())).encode()

    def headers(self, body: bytes, signature: str | None = "auto") -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Linear-Delivery": "delivery-1"}
        if signature == "auto":
            signature = sign(body)
        if signature is not None:
            headers["Linear-Signature"] = signature
        return headers

    def post(self, body: bytes, signature: str | None = "auto"):
        return self.client.post(
            "/webhooks/linear", content=body, headers=self.headers(body, signature)
        )

    def test_health(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_missing_signature(self) -> None:
        body = json.dumps(issue_payload(labels=[FEATURE], webhook_timestamp=now_ms())).encode()
        self.assertEqual(self.post(body, signature=None).status_code, 401)

    def test_invalid_signature(self) -> None:
        body = json.dumps(issue_payload(labels=[FEATURE], webhook_timestamp=now_ms())).encode()
        self.assertEqual(self.post(body, signature="deadbeef").status_code, 401)

    def test_invalid_json(self) -> None:
        self.assertEqual(self.post(b"{nope").status_code, 400)

    def test_stale_timestamp(self) -> None:
        body = json.dumps(
            issue_payload(labels=[FEATURE], webhook_timestamp=now_ms() - 120_000)
        ).encode()
        self.assertEqual(self.post(body).status_code, 400)

    def test_ignored(self) -> None:
        body = json.dumps(issue_payload(labels=[DOCS], webhook_timestamp=now_ms())).encode()
        with mock.patch("software_factory.linear.webhook.handle_issue") as handler:
            response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})
        handler.assert_not_called()
        self.assertEqual(self.temporal.started, [])

    def test_accepted_starts_workflow(self) -> None:
        response = self.post(self.accepted_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted"})
        ((args, kwargs),) = self.temporal.started
        issue_input = args[1]
        self.assertEqual(issue_input.identifier, "ENG-12")
        self.assertEqual(kwargs["id"], f"plan-issue-{issue_input.issue_id}")
        self.assertEqual(kwargs["task_queue"], "software-factory")

    def in_progress_body(self, labels, updated_from) -> bytes:
        payload = issue_payload(
            "update",
            labels=labels,
            updated_from=updated_from,
            state={"id": "s1", "name": "In Progress"},
            webhook_timestamp=now_ms(),
        )
        return json.dumps(payload).encode()

    def test_in_progress_starts_implement_workflow(self) -> None:
        response = self.post(self.in_progress_body([FEATURE], {"stateId": "s-old"}))
        self.assertEqual(response.json(), {"status": "accepted"})
        ((args, kwargs),) = self.temporal.started
        self.assertIs(args[0], ImplementIssueWorkflow.run)
        self.assertEqual(kwargs["id"], "implement-issue-issue-uuid")
        self.assertEqual(args[1].identifier, "ENG-12")

    def test_in_review_starts_review_workflow(self) -> None:
        payload = issue_payload(
            "update",
            labels=[FEATURE],
            updated_from={"stateId": "s-old"},
            state={"id": "s3", "name": "In Review"},
            webhook_timestamp=now_ms(),
        )
        response = self.post(json.dumps(payload).encode())
        self.assertEqual(response.json(), {"status": "accepted"})
        ((args, kwargs),) = self.temporal.started
        self.assertIs(args[0], ReviewIssueWorkflow.run)
        self.assertEqual(kwargs["id"], "review-issue-issue-uuid")
        self.assertEqual(args[1].identifier, "ENG-12")

    def test_label_added_and_in_progress_starts_plan(self) -> None:
        response = self.post(
            self.in_progress_body([FEATURE], {"labelIds": [], "stateId": "s-old"})
        )
        self.assertEqual(response.json(), {"status": "accepted"})
        ((args, kwargs),) = self.temporal.started
        self.assertIs(args[0], PlanIssueWorkflow.run)
        self.assertTrue(kwargs["id"].startswith("plan-issue-"))

    def test_in_progress_without_label_is_ignored(self) -> None:
        response = self.post(self.in_progress_body([DOCS], {"stateId": "s-old"}))
        self.assertEqual(response.json(), {"status": "ignored"})
        self.assertEqual(self.temporal.started, [])

    def test_already_started(self) -> None:
        temporal = FakeTemporalClient(WorkflowAlreadyStartedError("plan-issue-x", "PlanIssueWorkflow"))
        body = self.accepted_body()
        response = self.make_client(temporal).post(
            "/webhooks/linear", content=body, headers=self.headers(body)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "duplicate"})

    def test_temporal_unavailable(self) -> None:
        temporal = FakeTemporalClient(RuntimeError("conexão recusada"))
        body = self.accepted_body()
        response = self.make_client(temporal).post(
            "/webhooks/linear", content=body, headers=self.headers(body)
        )
        self.assertEqual(response.status_code, 503)


    def post_comment(self, **kwargs):
        body = json.dumps(comment_payload(webhook_timestamp=now_ms(), **kwargs)).encode()
        return self.post(body)

    def test_reply_signals_implement_and_review_workflows(self) -> None:
        response = self.post_comment()
        self.assertEqual(response.json(), {"status": "accepted"})
        answer = HumanAnswer("question-1", "comment-1", "Use bcrypt")
        self.assertEqual(
            self.temporal.signals,
            [
                ("implement-issue-issue-uuid", ImplementIssueWorkflow.human_answer, answer),
                ("review-issue-issue-uuid", ReviewIssueWorkflow.human_answer, answer),
            ],
        )

    def test_reply_signals_review_workflow_when_implement_not_found(self) -> None:
        self.temporal.signal_errors = {
            "implement-issue-issue-uuid": RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        }
        response = self.post_comment()
        self.assertEqual(response.json(), {"status": "accepted"})
        ((wf_id, signal, _),) = self.temporal.signals
        self.assertEqual(wf_id, "review-issue-issue-uuid")
        self.assertEqual(signal, ReviewIssueWorkflow.human_answer)

    def test_reply_without_any_workflow_is_ignored(self) -> None:
        not_found = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        self.temporal.signal_errors = {
            "implement-issue-issue-uuid": not_found,
            "review-issue-issue-uuid": not_found,
        }
        response = self.post_comment()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

    def test_reply_with_temporal_down_is_503(self) -> None:
        self.temporal.signal_error = RuntimeError("down")
        self.assertEqual(self.post_comment().status_code, 503)

    def test_top_level_comment_is_ignored(self) -> None:
        response = self.post_comment(parent_id=None)
        self.assertEqual(response.json(), {"status": "ignored"})
        self.assertEqual(self.temporal.signals, [])


if __name__ == "__main__":
    unittest.main()
