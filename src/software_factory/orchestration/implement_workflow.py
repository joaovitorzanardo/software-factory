import asyncio
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
        OUTCOME_STOPPED,
        STATE_IN_PLANNING,
        STATE_IN_PROGRESS,
        STATE_IN_REVIEW,
        AnalyzeRequest,
        Clarification,
        CloneBranchRequest,
        CommentRequest,
        CommitImplementationRequest,
        FixAnalysis,
        FixAttempt,
        FixRequest,
        HumanAnswer,
        ImplementRequest,
        ImplementResult,
        MoveIssueRequest,
        PlanIssueInput,
        PullRequestRequest,
        TestRunResult,
    )

# Correções seguidas sem verde antes de perguntar no Linear como seguir (trava de custo).
MAX_FIX_ATTEMPTS = 5
# Só as últimas tentativas vão para o prompt da análise.
ATTEMPTS_IN_PROMPT = 3
HUMAN_ANSWER_TIMEOUT = timedelta(days=7)
# Durante a espera, confere a cada fatia se a issue segue em In Progress.
ANSWER_POLL_INTERVAL = timedelta(hours=1)
# Checagem de status no loop e na espera: tentativas ilimitadas, para uma instabilidade do
# Linear não derrubar dias de espera. ApplicationError não retentável ainda falha o workflow.
STATUS_CHECK_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=5))


def format_question(question: str) -> str:
    return (
        "**software-factory precisa de uma decisão** para seguir com a correção dos testes.\n"
        "\n"
        f"{question}\n"
        "\n"
        "_Responda **nesta thread** (reply a este comentário) para o workflow continuar. "
        "Para parar, mova a issue para fora de In Progress._"
    )


def stuck_question(n: int, last: FixAttempt, output: str) -> str:
    return (
        f"Fiz {n} tentativas de correção e os testes continuam vermelhos.\n\n"
        f"**Última análise:**\n{last.analysis[:3000]}\n\n"
        f"**Saída dos testes:**\n```\n{output[-2000:]}\n```\n\n"
        "Como devo seguir?"
    )


@workflow.defn
class ImplementIssueWorkflow:
    """Issue em In Progress: implementa o plano de factory/<issue> num sandbox e roda os testes.
    Cada sessão (implementação, análise, correção) usa um sandbox próprio e empurra um
    checkpoint para factory/<issue>. Com testes vermelhos, entra no loop analisar → planejar →
    corrigir até ficarem verdes; decisões grandes (ou MAX_FIX_ATTEMPTS correções seguidas sem
    verde) viram pergunta no Linear, respondida na thread do comentário. Verde: abre a PR e move
    a issue para In Review. Sem plano, volta para In Planning. Issue fora de In Progress: para
    (OUTCOME_STOPPED), sem PR.

    Em falha (inclusive sem resposta em HUMAN_ANSWER_TIMEOUT) a issue fica em In Progress
    (sem compensação)."""

    def __init__(self) -> None:
        self._plan_sha: str | None = None
        self._clarifications: list[Clarification] = []
        self._pending_question: str | None = None
        self._answer: str | None = None

    @workflow.signal
    def human_answer(self, answer: HumanAnswer) -> None:
        # Só vale reply à pergunta pendente, e só o primeiro.
        if answer.question_id == self._pending_question and self._answer is None:
            self._answer = answer.body

    async def _move(self, request: MoveIssueRequest) -> bool:
        return await workflow.execute_activity(
            "move_issue",
            request,
            start_to_close_timeout=MOVE_ISSUE_TIMEOUT,
            retry_policy=MOVE_ISSUE_RETRY,
            result_type=bool,
        )

    async def _still_in_progress(self, issue: PlanIssueInput) -> bool:
        return await workflow.execute_activity(
            "move_issue",
            MoveIssueRequest(
                issue.issue_id, to_state=STATE_IN_PROGRESS, from_state=STATE_IN_PROGRESS
            ),
            start_to_close_timeout=MOVE_ISSUE_TIMEOUT,
            retry_policy=STATUS_CHECK_RETRY,
            result_type=bool,
        )

    async def _create_sandbox(self, issue: PlanIssueInput) -> str:
        return await workflow.execute_activity(
            "create_sandbox",
            issue.issue_id,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3),
            result_type=str,
        )

    async def _clone_branch(self, sandbox_id: str, issue: PlanIssueInput) -> str:
        return await workflow.execute_activity(
            "clone_branch",
            CloneBranchRequest(sandbox_id=sandbox_id, identifier=issue.identifier),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
            result_type=str,
        )

    async def _kill_sandbox(self, sandbox_id: str) -> None:
        await workflow.execute_activity(
            "kill_sandbox",
            sandbox_id,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _implement_session(
        self, issue: PlanIssueInput, attempt: int, fix: FixAnalysis | None = None
    ) -> TestRunResult:
        """Implementa o plano (attempt 0) ou a correção, roda os testes e empurra o resultado:
        commit final se verde, checkpoint `wip` se vermelho."""
        sandbox_id = await self._create_sandbox(issue)
        try:
            base_sha = await self._clone_branch(sandbox_id, issue)
            if self._plan_sha is None:
                self._plan_sha = base_sha
            # Uma tentativa só: um retry não caberia na vida do sandbox (~55 min).
            if fix is None:
                await workflow.execute_activity(
                    "implement_plan",
                    ImplementRequest(sandbox_id=sandbox_id, issue=issue),
                    start_to_close_timeout=timedelta(minutes=35),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    result_type=str,
                )
            else:
                await workflow.execute_activity(
                    "implement_fix",
                    FixRequest(
                        sandbox_id=sandbox_id,
                        issue=issue,
                        analysis=fix.analysis,
                        plan=fix.plan or "",
                        clarifications=tuple(self._clarifications),
                    ),
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
            if tests.passed:
                message = f"feat: implementa {issue.identifier}"
            else:
                message = f"wip: tentativa {attempt} de {issue.identifier} (testes vermelhos)"
            await workflow.execute_activity(
                "commit_implementation",
                CommitImplementationRequest(
                    sandbox_id=sandbox_id,
                    identifier=issue.identifier,
                    base_sha=base_sha,
                    message=message,
                    # Exige mudança só no verde direto sobre o plano: depois de um checkpoint,
                    # uma correção sem mudança ainda vale.
                    require_changes=tests.passed and base_sha == self._plan_sha,
                ),
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
                result_type=str,
            )
            return tests
        finally:
            await self._kill_sandbox(sandbox_id)

    async def _analysis_session(
        self, issue: PlanIssueInput, test_output: str, attempts: list[FixAttempt]
    ) -> FixAnalysis:
        sandbox_id = await self._create_sandbox(issue)
        try:
            await self._clone_branch(sandbox_id, issue)
            return await workflow.execute_activity(
                "analyze_failure",
                AnalyzeRequest(
                    sandbox_id=sandbox_id,
                    issue=issue,
                    test_output=test_output,
                    attempts=tuple(attempts),
                    clarifications=tuple(self._clarifications),
                ),
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=FixAnalysis,
            )
        finally:
            await self._kill_sandbox(sandbox_id)

    async def _ask(self, issue: PlanIssueInput, question: str) -> bool:
        """Pergunta no Linear e espera o reply na thread. False = issue saiu de In Progress."""
        comment_id = str(workflow.uuid4())
        # Antes do post: um reply rápido já encontra a pergunta pendente.
        self._pending_question = comment_id
        self._answer = None
        await workflow.execute_activity(
            "post_comment",
            CommentRequest(issue.issue_id, comment_id, format_question(question)),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=str,
        )
        deadline = workflow.now() + HUMAN_ANSWER_TIMEOUT
        while self._answer is None:
            remaining = deadline - workflow.now()
            if remaining <= timedelta(0):
                raise ApplicationError(
                    f"{issue.identifier}: sem resposta em {HUMAN_ANSWER_TIMEOUT.days} dias",
                    non_retryable=True,
                )
            try:
                await workflow.wait_condition(
                    lambda: self._answer is not None,
                    timeout=min(ANSWER_POLL_INTERVAL, remaining),
                )
            except asyncio.TimeoutError:
                if not await self._still_in_progress(issue):
                    self._pending_question = None
                    return False
        self._clarifications.append(Clarification(question, self._answer))
        self._pending_question = None
        return True

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
        attempts: list[FixAttempt] = []
        round_start = 0
        while not tests.passed:
            if not await self._still_in_progress(issue):
                workflow.logger.info(
                    "%s saiu de %s; para a correção", issue.identifier, STATE_IN_PROGRESS
                )
                return ImplementResult(issue.identifier, OUTCOME_STOPPED)
            if len(attempts) - round_start >= MAX_FIX_ATTEMPTS:
                question = stuck_question(len(attempts) - round_start, attempts[-1], tests.output)
                if not await self._ask(issue, question):
                    return ImplementResult(issue.identifier, OUTCOME_STOPPED)
                round_start = len(attempts)
            analysis = await self._analysis_session(
                issue, tests.output, attempts[-ATTEMPTS_IN_PROMPT:]
            )
            if analysis.question:
                if not await self._ask(issue, analysis.question):
                    return ImplementResult(issue.identifier, OUTCOME_STOPPED)
                # Reanalisa com a resposta; pergunta não conta tentativa.
                continue
            tests = await self._implement_session(issue, attempt=len(attempts) + 1, fix=analysis)
            attempts.append(FixAttempt(len(attempts) + 1, analysis.analysis, tests.output))

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
