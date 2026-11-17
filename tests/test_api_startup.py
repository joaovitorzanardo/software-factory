import unittest

from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.api import create_app
from software_factory.config import Settings
from software_factory.orchestration.reconcile_workflow import ReconcileIssuesWorkflow
from tests.test_webhook import FakeTemporalClient


def run_app(temporal: FakeTemporalClient) -> int:
    """Sobe o app (o `with` roda o lifespan) e devolve o status do /health."""
    app = create_app(Settings(linear_webhook_secret="s"), temporal_client=temporal)
    with TestClient(app) as client:
        return client.get("/health").status_code


class TestStartupReconcile(unittest.TestCase):
    def test_startup_starts_reconcile(self) -> None:
        temporal = FakeTemporalClient()
        self.assertEqual(run_app(temporal), 200)
        ((args, kwargs),) = temporal.started
        self.assertIs(args[0], ReconcileIssuesWorkflow.run)
        self.assertEqual(args[1].trigger_labels, ("bug", "feature"))
        self.assertEqual(kwargs["id"], "reconcile-issues")
        self.assertEqual(kwargs["task_queue"], "software-factory")

    def test_startup_survives_already_started(self) -> None:
        temporal = FakeTemporalClient(WorkflowAlreadyStartedError("reconcile-issues", "x"))
        self.assertEqual(run_app(temporal), 200)

    def test_startup_survives_temporal_error(self) -> None:
        self.assertEqual(run_app(FakeTemporalClient(RuntimeError("temporal fora"))), 200)
