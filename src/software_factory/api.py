import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory import __version__
from software_factory.config import Settings
from software_factory.linear.webhook import router as linear_router
from software_factory.orchestration.models import ReconcileInput
from software_factory.orchestration.reconcile_workflow import (
    RECONCILE_WORKFLOW_ID,
    ReconcileIssuesWorkflow,
)

logger = logging.getLogger(__name__)


async def start_reconcile(client: Any, settings: Settings) -> None:
    """Inicia a varredura de issues perdidas com o servidor fora; falha não impede a subida."""
    try:
        await client.start_workflow(
            ReconcileIssuesWorkflow.run,
            ReconcileInput(trigger_labels=tuple(sorted(settings.trigger_labels))),
            id=RECONCILE_WORKFLOW_ID,
            task_queue=settings.task_queue,
        )
    except WorkflowAlreadyStartedError:
        logger.info("reconciliação %s já em execução", RECONCILE_WORKFLOW_ID)
    except Exception:
        logger.exception("falha ao iniciar a reconciliação %s", RECONCILE_WORKFLOW_ID)
    else:
        logger.info("reconciliação %s iniciada", RECONCILE_WORKFLOW_ID)


def create_app(settings: Settings | None = None, temporal_client: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.temporal is None:
            app.state.temporal = await Client.connect(settings.temporal_address)
        await start_reconcile(app.state.temporal, settings)
        yield

    app = FastAPI(title="software-factory", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.temporal = temporal_client
    app.include_router(linear_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
