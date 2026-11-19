import unittest
import uuid
from typing import Any

from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.notify.email import USAGE_ALERT_SUBJECT, EmailSettings
from software_factory.orchestration.models import UsageAlert
from software_factory.orchestration.notify_activities import NotifyActivities, UsageAlertNotifier
from software_factory.orchestration.usage_alert_workflow import UsageAlertWorkflow

RESET = 1795440000
ALERT = UsageAlert("five_hour", 0.85, RESET, "implement-issue-abc/implement_plan")
EMAIL = EmailSettings(
    host="smtp.example.com",
    port=587,
    username="bot@example.com",
    password="pw",
    sender="bot@example.com",
    to="dev@example.com",
)


class TestUsageAlertWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_sends_alert(self) -> None:
        received: list[UsageAlert] = []

        @activity.defn(name="send_usage_alert")
        async def send_usage_alert(alert: UsageAlert) -> None:
            received.append(alert)

        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[UsageAlertWorkflow],
                activities=[send_usage_alert],
            ):
                await env.client.execute_workflow(
                    UsageAlertWorkflow.run, ALERT, id=f"alert-{uuid.uuid4()}", task_queue=task_queue
                )
        self.assertEqual(received, [ALERT])


class TestNotifyActivities(unittest.IsolatedAsyncioTestCase):
    async def test_activity_formats_and_sends(self) -> None:
        sent: list[tuple[EmailSettings, str, str]] = []

        def fake_send(settings: EmailSettings, subject: str, body: str) -> None:
            sent.append((settings, subject, body))

        notify = NotifyActivities(EMAIL, send=fake_send)
        await ActivityEnvironment().run(notify.send_usage_alert, ALERT)
        [(settings, subject, body)] = sent
        self.assertEqual(settings, EMAIL)
        self.assertEqual(subject, USAGE_ALERT_SUBJECT)
        self.assertIn("85% de uso", body)


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error


class TestUsageAlertNotifier(unittest.IsolatedAsyncioTestCase):
    async def test_notifier_dedups_unless_failed(self) -> None:
        client = FakeClient()
        self.assertTrue(await UsageAlertNotifier(client, "factory-queue").notify(ALERT))
        [(args, kwargs)] = client.calls
        self.assertEqual(args, (UsageAlertWorkflow.run, ALERT))
        self.assertEqual(kwargs["id"], "usage-alert-five-hour-1795440000")
        self.assertEqual(kwargs["id_reuse_policy"], WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        self.assertEqual(kwargs["task_queue"], "factory-queue")

    async def test_notifier_duplicate(self) -> None:
        client = FakeClient(WorkflowAlreadyStartedError("id", "UsageAlertWorkflow"))
        self.assertFalse(await UsageAlertNotifier(client, "q").notify(ALERT))

    async def test_notifier_swallows_errors(self) -> None:
        client = FakeClient(RuntimeError("temporal fora"))
        with self.assertLogs("software_factory.orchestration.notify_activities", "WARNING"):
            self.assertFalse(await UsageAlertNotifier(client, "q").notify(ALERT))


if __name__ == "__main__":
    unittest.main()
