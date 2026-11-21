"""Publicação do relatório de tokens no Linear, para uso dentro de workflows (sem I/O)."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import CommentRequest, UsageReportRequest
    from software_factory.orchestration.token_usage import token_usage_comment_id

REPORT_TIMEOUT = timedelta(seconds=30)
REPORT_RETRY = RetryPolicy(maximum_attempts=3)
UPSERT_TIMEOUT = timedelta(seconds=30)
UPSERT_RETRY = RetryPolicy(maximum_attempts=5)


async def publish_token_usage(issue_id: str, final: bool = False) -> None:
    """Cria ou atualiza o comentário de tokens da issue; falha vira warning, nunca derruba o workflow."""
    try:
        body: str = await workflow.execute_activity(
            "token_usage_report",
            UsageReportRequest(issue_id, final),
            result_type=str,
            start_to_close_timeout=REPORT_TIMEOUT,
            retry_policy=REPORT_RETRY,
        )
        await workflow.execute_activity(
            "upsert_comment",
            CommentRequest(issue_id, token_usage_comment_id(issue_id), body),
            result_type=str,
            start_to_close_timeout=UPSERT_TIMEOUT,
            retry_policy=UPSERT_RETRY,
        )
    except ActivityError as error:
        workflow.logger.warning(
            "relatório de tokens da issue %s não publicado: %s", issue_id, error.cause or error
        )
