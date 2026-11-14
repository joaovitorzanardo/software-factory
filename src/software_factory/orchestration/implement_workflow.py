from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import (
        OUTCOME_NO_PLAN,
        OUTCOME_PR_OPENED,
        OUTCOME_SKIPPED,
        OUTCOME_STOPPED,
        STATE_IN_PLANNING,
        STATE_IN_PROGRESS,
        STATE_IN_REVIEW,
        HumanAnswer,
        ImplementResult,
        MoveIssueRequest,
        PlanIssueInput,
        PullRequestRequest,
    )
    from software_factory.orchestration.sessions import (  # noqa: F401 (reexportados)
        ANALYSIS_CAP,
        ANSWER_POLL_INTERVAL,
        ATTEMPTS_IN_PROMPT,
        HUMAN_ANSWER_TIMEOUT,
        MAX_FIX_ATTEMPTS,
        STATUS_CHECK_RETRY,
        STUCK_QUESTION,
        SessionWorkflow,
        failure_message,
        format_question,
        stuck_question,
    )


@workflow.defn
class ImplementIssueWorkflow(SessionWorkflow):
    """Issue em In Progress: implementa o plano de factory/<issue> num sandbox e roda os testes.
    Cada sessão (implementação, análise, correção) usa um sandbox próprio e empurra um
    checkpoint para factory/<issue>. Com testes vermelhos, entra no loop analisar → planejar →
    corrigir até ficarem verdes; decisões grandes (ou MAX_FIX_ATTEMPTS correções seguidas sem
    verde) viram pergunta no Linear, respondida na thread do comentário. Verde: abre a PR e move
    a issue para In Review. Sem plano, volta para In Planning. Issue fora de In Progress: para
    (OUTCOME_STOPPED), sem PR.

    Em falha (inclusive sem resposta em HUMAN_ANSWER_TIMEOUT) a issue fica em In Progress
    (sem compensação)."""


    @workflow.signal
    def human_answer(self, answer: HumanAnswer) -> None:
        self._receive_answer(answer)

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


        tests = await self._implement_session(issue, attempt=0)
        green = await self._fix_until_green(issue, tests, STATE_IN_PROGRESS)
        if green is None:
            return ImplementResult(issue.identifier, OUTCOME_STOPPED)


        # Verde: o commit final já foi empurrado pela última sessão.
        pr_url: str = await workflow.execute_activity(
            "open_pull_request",
            PullRequestRequest(identifier=issue.identifier, title=issue.title, url=issue.url),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=str,
        )
        await self._move(MoveIssueRequest(issue.issue_id, to_state=STATE_IN_REVIEW))
        return ImplementResult(issue.identifier, OUTCOME_PR_OPENED, pr_url)
