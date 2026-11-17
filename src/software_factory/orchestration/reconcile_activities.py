import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity

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

    @activity.defn
    async def start_missed_workflow(self, pending: PendingIssue) -> bool:
        """Inicia o workflow se nenhum run dele começou depois do gatilho; True se iniciou."""
        workflow_id = issue_workflow_id(pending.kind, pending.issue.issue_id)
        since = datetime.fromisoformat(pending.triggered_at).astimezone(UTC) - CLOCK_SKEW_TOLERANCE
        query = f"WorkflowId = '{workflow_id}' AND StartTime >= '{since:%Y-%m-%dT%H:%M:%SZ}'"
        if (await self.temporal.count_workflows(query)).count > 0:
            logger.info(
                "%s já rodou depois do gatilho (%s); não inicia", workflow_id, pending.triggered_at
            )
            return False
        return await start_issue_workflow(
            self.temporal, self.task_queue, pending.kind, pending.issue, "reconciliação"
        )
