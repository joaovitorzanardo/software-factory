"""Aviso de uso da sessão do Claude: activity de envio e notifier que inicia o workflow."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.notify.email import EmailSettings, build_usage_alert_email, send_email
from software_factory.orchestration.models import UsageAlert
from software_factory.orchestration.usage import usage_alert_workflow_id
from software_factory.orchestration.usage_alert_workflow import UsageAlertWorkflow

logger = logging.getLogger(__name__)


class NotifyActivities:
    def __init__(self, email: EmailSettings, send: Callable[..., None] = send_email) -> None:
        self.email = email
        self.send = send

    @activity.defn
    async def send_usage_alert(self, alert: UsageAlert) -> None:
        # SMTP não tem idempotência: um retry depois de um envio que deu certo duplica o email.
        subject, body = build_usage_alert_email(alert, self.email.timezone)
        await asyncio.to_thread(self.send, self.email, subject, body)


class UsageAlertNotifier:
    """Inicia um UsageAlertWorkflow por janela; nunca levanta (o aviso não derruba a sessão)."""

    def __init__(self, temporal: Any, task_queue: str) -> None:
        self.temporal = temporal
        self.task_queue = task_queue

    async def notify(self, alert: UsageAlert) -> bool:
        workflow_id = usage_alert_workflow_id(alert)
        try:
            await self.temporal.start_workflow(
                UsageAlertWorkflow.run,
                alert,
                id=workflow_id,
                task_queue=self.task_queue,
                # Rejeita também depois do run terminar: um email por janela.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            logger.info("aviso de uso %s já enviado ou em envio", workflow_id)
            return False
        except Exception:
            logger.warning("falha ao iniciar o aviso de uso %s", workflow_id, exc_info=True)
            return False
        logger.info("aviso de uso %s iniciado (%.0f%%)", workflow_id, alert.utilization * 100)
        return True
