from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.dispatch import issue_workflow_id
    from software_factory.orchestration.models import (
        PendingIssue,
        ReconcileInput,
        ReconcileResult,
    )

RECONCILE_WORKFLOW_ID = "reconcile-issues"


@workflow.defn
class ReconcileIssuesWorkflow:
    """Inicia os workflows de issue cujo webhook se perdeu (servidor fora).

    Uma issue cujo `start_missed_workflow` esgota os retries falha o workflow, sem compensação."""

    @workflow.run
    async def run(self, request: ReconcileInput) -> ReconcileResult:
        pendings: list[PendingIssue] = await workflow.execute_activity(
            "list_pending_issues",
            request,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=list[PendingIssue],
        )
        started: list[str] = []
        skipped = 0
        for pending in pendings:
            ok: bool = await workflow.execute_activity(
                "start_missed_workflow",
                pending,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
                result_type=bool,
            )
            if ok:
                started.append(issue_workflow_id(pending.kind, pending.issue.issue_id))
            else:
                skipped += 1
        workflow.logger.info("reconciliação: %d iniciados, %d pulados", len(started), skipped)
        return ReconcileResult(started=started, skipped=skipped)
