import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

from software_factory.linear.client import LinearClient
from software_factory.orchestration.dispatch import issue_workflow_id, start_issue_workflow
from software_factory.orchestration.models import (
    STATE_IN_PROGRESS,
    STATE_IN_REVIEW,
    STATE_TODO,
    PendingIssue,
    ReconcileInput,
)
from software_factory.orchestration.reconcile import pending_issue

logger = logging.getLogger(__name__)

# O run iniciado pelo próprio webhook começa segundos depois do `createdAt` do Linear; com o
# relógio local atrasado, sem folga, ele pareceria anterior ao gatilho.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=2)
ISSUES_PAGE_SIZE = 50
# `history` vem do mais novo para o mais antigo: com mais de 50 eventos depois da última entrada
# no status, o gatilho cai num evento mais antigo (ou no createdAt). Isso só faz pular a issue,
# nunca reexecutar.
HISTORY_PAGE_SIZE = 50

PENDING_ISSUES_QUERY = f"""
query($after: String) {{
  issues(
    first: {ISSUES_PAGE_SIZE}
    after: $after
    filter: {{ or: [
      {{ state: {{ name: {{ eqIgnoreCase: "{STATE_TODO}" }} }} }}
      {{ state: {{ name: {{ eqIgnoreCase: "{STATE_IN_PROGRESS}" }} }} }}
      {{ state: {{ name: {{ eqIgnoreCase: "{STATE_IN_REVIEW}" }} }} }}
    ] }}
  ) {{
    nodes {{
      id identifier title description url createdAt
      state {{ name }}
      labels {{ nodes {{ id name }} }}
      history(first: {HISTORY_PAGE_SIZE}) {{ nodes {{ createdAt addedLabelIds toState {{ name }} }} }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


class ReconcileActivities:
    def __init__(self, linear: LinearClient, temporal: Any, task_queue: str) -> None:
        self.linear = linear
        self.temporal = temporal
        self.task_queue = task_queue

    @activity.defn
    async def list_pending_issues(self, request: ReconcileInput) -> list[PendingIssue]:
        """Issues com label de gatilho em To Do, In Progress ou In Review, com o gatilho."""
        pending: list[PendingIssue] = []
        after: str | None = None
        while True:
            data = await self.linear.execute(PENDING_ISSUES_QUERY, {"after": after})
            issues = data["issues"]
            for node in issues["nodes"]:
                found = pending_issue(node, request.trigger_labels)
                if found is not None:
                    pending.append(found)
            if not issues["pageInfo"]["hasNextPage"]:
                return pending
            after = issues["pageInfo"]["endCursor"]

    async def _retention(self) -> timedelta:
        response = await self.temporal.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=self.temporal.namespace)
        )
        return response.config.workflow_execution_retention_ttl.ToTimedelta()

    @activity.defn
    async def start_missed_workflow(self, pending: PendingIssue) -> bool:
        """Inicia o workflow se nenhum run dele começou depois do gatilho; True se iniciou.

        Gatilho mais antigo que a retenção do namespace é pulado: um run fechado já pode ter
        saído da visibility, e reexecutar (ex.: implementação que falhou) custa caro."""
        workflow_id = issue_workflow_id(pending.kind, pending.issue.issue_id)
        triggered = datetime.fromisoformat(pending.triggered_at).astimezone(UTC)
        retention = await self._retention()
        if triggered < datetime.now(UTC) - retention:
            logger.warning(
                "%s: gatilho %s é mais antigo que a retenção do Temporal (%s); não inicia",
                workflow_id,
                pending.triggered_at,
                retention,
            )
            return False
        since = triggered - CLOCK_SKEW_TOLERANCE
        query = f"WorkflowId = '{workflow_id}' AND StartTime >= '{since:%Y-%m-%dT%H:%M:%SZ}'"
        if (await self.temporal.count_workflows(query)).count > 0:
            logger.info(
                "%s já rodou depois do gatilho (%s); não inicia", workflow_id, pending.triggered_at
            )
            return False
        return await start_issue_workflow(
            self.temporal, self.task_queue, pending.kind, pending.issue, "reconciliação"
        )
