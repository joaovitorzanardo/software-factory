"""Resolução do repo alvo da issue, para uso dentro de workflows (sem I/O)."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import (
        CommentRequest,
        PlanIssueInput,
        RepoResolution,
        RepoTarget,
    )

RESOLVE_TIMEOUT = timedelta(seconds=60)
RESOLVE_RETRY = RetryPolicy(maximum_attempts=5)
COMMENT_TIMEOUT = timedelta(seconds=30)
COMMENT_RETRY = RetryPolicy(maximum_attempts=5)


def repo_problem_comment(problem: str) -> str:
    return (
        "**software-factory não sabe em qual repo trabalhar.**\n"
        "\n"
        f"{problem}\n"
        "\n"
        "_Corrija (link do repo nos Resources do projeto, ou `.factory.toml` na branch default "
        "do repo) e dispare a etapa de novo._"
    )


async def resolve_target(issue: PlanIssueInput) -> RepoTarget:
    """Repo e comando de testes da issue; sem eles, comenta o motivo na issue e falha o workflow."""
    resolution: RepoResolution = await workflow.execute_activity(
        "resolve_repo",
        issue,
        start_to_close_timeout=RESOLVE_TIMEOUT,
        retry_policy=RESOLVE_RETRY,
        result_type=RepoResolution,
    )
    if resolution.target is not None and not resolution.problem:
        return resolution.target
    problem = resolution.problem or "o repo da issue não foi resolvido"
    await workflow.execute_activity(
        "post_comment",
        CommentRequest(issue.issue_id, str(workflow.uuid4()), repo_problem_comment(problem)),
        start_to_close_timeout=COMMENT_TIMEOUT,
        retry_policy=COMMENT_RETRY,
        result_type=str,
    )
    raise ApplicationError(f"{issue.identifier}: {problem}", non_retryable=True)
