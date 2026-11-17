"""Início dos workflows de issue, compartilhado pelo webhook e pela reconciliação."""

import logging
from collections.abc import Callable
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import PlanIssueInput
from software_factory.orchestration.review_workflow import ReviewIssueWorkflow
from software_factory.orchestration.workflow import PlanIssueWorkflow

logger = logging.getLogger(__name__)

KIND_PLAN = "plan"
KIND_IMPLEMENT = "implement"
KIND_REVIEW = "review"

# kind → (método run do workflow, prefixo do id)
ISSUE_WORKFLOWS: dict[str, tuple[Callable, str]] = {
    KIND_PLAN: (PlanIssueWorkflow.run, "plan-issue"),
    KIND_IMPLEMENT: (ImplementIssueWorkflow.run, "implement-issue"),
    KIND_REVIEW: (ReviewIssueWorkflow.run, "review-issue"),
}


def issue_workflow_id(kind: str, issue_id: str) -> str:
    return f"{ISSUE_WORKFLOWS[kind][1]}-{issue_id}"


async def start_issue_workflow(
    client: Any, task_queue: str, kind: str, issue: PlanIssueInput, context: str = ""
) -> bool:
    """Inicia o workflow `kind` da issue; False se já há um rodando para ela."""
    workflow_run = ISSUE_WORKFLOWS[kind][0]
    workflow_id = issue_workflow_id(kind, issue.issue_id)
    try:
        await client.start_workflow(workflow_run, issue, id=workflow_id, task_queue=task_queue)
    except WorkflowAlreadyStartedError:
        logger.info("workflow %s já em execução; %s", workflow_id, context)
        return False
    logger.info(
        "workflow %s iniciado: %s %r; %s", workflow_id, issue.identifier, issue.title, context
    )
    return True
