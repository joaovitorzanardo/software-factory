from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from software_factory.orchestration.workflow import MOVE_ISSUE_RETRY, MOVE_ISSUE_TIMEOUT

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import (
        OUTCOME_NO_PLAN,
        OUTCOME_PR_OPENED,
        OUTCOME_SKIPPED,
        STATE_IN_PLANNING,
        STATE_IN_PROGRESS,
        STATE_IN_REVIEW,
        CloneBranchRequest,
        CommitImplementationRequest,
        ImplementRequest,
        ImplementResult,
        MoveIssueRequest,
        PlanIssueInput,
        PullRequestRequest,
        TestRunResult,
    )


@workflow.defn
class ImplementIssueWorkflow:
    """Issue em In Progress: implementa o plano de factory/<issue> num sandbox, roda os testes e,
    só se verdes, faz push, abre a PR e move a issue para In Review. Sem plano, volta para
    In Planning.

    Em falha a issue fica em In Progress (sem compensação)."""

    async def _move(self, request: MoveIssueRequest) -> bool:
        return await workflow.execute_activity(
            "move_issue",
            request,
            start_to_close_timeout=MOVE_ISSUE_TIMEOUT,
            retry_policy=MOVE_ISSUE_RETRY,
            result_type=bool,
        )

    @workflow.run
    async def run(self, issue: PlanIssueInput) -> ImplementResult:
        # Não move nada: confirma que segue em In Progress e valida os status usados adiante.
        still_in_progress = await self._move(
            MoveIssueRequest(
                issue.issue_id,
                to_state=STATE_IN_PROGRESS,
                from_state=STATE_IN_PROGRESS,
                require_states=(STATE_IN_REVIEW, STATE_IN_PLANNING),
            )
        )
        if not still_in_progress:
            workflow.logger.info("%s saiu de %s; não implementa", issue.identifier, STATE_IN_PROGRESS)
            return ImplementResult(issue.identifier, OUTCOME_SKIPPED)

        # Checado antes de gastar um sandbox.
        has_plan: bool = await workflow.execute_activity(
            "plan_exists",
            issue.identifier,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=bool,
        )
        if not has_plan:
            workflow.logger.info("%s sem plano; volta para %s", issue.identifier, STATE_IN_PLANNING)
            await self._move(
                MoveIssueRequest(
                    issue.issue_id, to_state=STATE_IN_PLANNING, from_state=STATE_IN_PROGRESS
                )
            )
            return ImplementResult(issue.identifier, OUTCOME_NO_PLAN)

        sandbox_id: str = await workflow.execute_activity(
            "create_sandbox",
            issue.issue_id,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3),
            result_type=str,
        )
        try:
            base_sha: str = await workflow.execute_activity(
                "clone_branch",
                CloneBranchRequest(sandbox_id=sandbox_id, identifier=issue.identifier),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
                result_type=str,
            )
            # Uma tentativa só: um retry não caberia na vida do sandbox (~55 min).
            await workflow.execute_activity(
                "implement_plan",
                ImplementRequest(sandbox_id=sandbox_id, issue=issue),
                start_to_close_timeout=timedelta(minutes=35),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=str,
            )
            tests: TestRunResult = await workflow.execute_activity(
                "run_tests",
                sandbox_id,
                start_to_close_timeout=timedelta(minutes=12),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=TestRunResult,
            )
            if not tests.passed:
                raise ApplicationError(f"testes falharam:\n{tests.output}", non_retryable=True)
            await workflow.execute_activity(
                "commit_implementation",
                CommitImplementationRequest(
                    sandbox_id=sandbox_id, identifier=issue.identifier, base_sha=base_sha
                ),
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
                result_type=str,
            )
        finally:
            await workflow.execute_activity(
                "kill_sandbox",
                sandbox_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        pr_url: str = await workflow.execute_activity(
            "open_pull_request",
            PullRequestRequest(identifier=issue.identifier, title=issue.title, url=issue.url),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=str,
        )
        await self._move(MoveIssueRequest(issue.issue_id, to_state=STATE_IN_REVIEW))
        return ImplementResult(issue.identifier, OUTCOME_PR_OPENED, pr_url)
