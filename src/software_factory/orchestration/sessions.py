import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError

from software_factory.orchestration.workflow import MOVE_ISSUE_RETRY, MOVE_ISSUE_TIMEOUT

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import (
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
        MoveIssueRequest,
        PlanIssueInput,
        TestRunResult,
    )

# Correções seguidas sem verde antes de perguntar no Linear como seguir (trava de custo).
MAX_FIX_ATTEMPTS = 5
# Só as últimas tentativas vão para o prompt da análise.
ATTEMPTS_IN_PROMPT = 3
HUMAN_ANSWER_TIMEOUT = timedelta(days=7)
# Durante a espera, confere a cada fatia se a issue segue no status da sessão.
ANSWER_POLL_INTERVAL = timedelta(hours=1)
# Checagem de status no loop e na espera: tentativas ilimitadas, para uma instabilidade do
# Linear não derrubar dias de espera. ApplicationError não retentável ainda falha o workflow.
STATUS_CHECK_RETRY = RetryPolicy(maximum_interval=timedelta(minutes=5))
ANALYSIS_CAP = 3000
# Rótulo curto da pergunta de travamento nas clarificações (o comentário leva o texto inteiro).
STUCK_QUESTION = (
    "Fiz {n} tentativas de correção e os testes continuam vermelhos. Como devo seguir?"
)


def failure_message(error: ActivityError) -> str:
    """Mensagem da causa (ApplicationError, timeout...) de uma activity que falhou.
    Cancelamento não vira tentativa: propaga."""
    if isinstance(error.cause, CancelledError):
        raise error
    return str(error.cause or error)[:2000]


def format_question(question: str, state: str) -> str:
    return (
        "**software-factory precisa de uma decisão** para seguir.\n"
        "\n"
        f"{question}\n"
        "\n"
        "_Responda **nesta thread** (reply a este comentário) para o workflow continuar. "
        f"Para parar, mova a issue para fora de {state}._"
    )


def stuck_question(n: int, last: FixAttempt, output: str) -> str:
    return (
        f"Fiz {n} tentativas de correção e os testes continuam vermelhos.\n\n"
        f"**Última análise:**\n{last.analysis[:ANALYSIS_CAP]}\n\n"
        f"**Saída dos testes:**\n```\n{output[-2000:]}\n```\n\n"
        "Como devo seguir?"
    )


class SessionWorkflow:
    """Base das workflows que rodam sessões do Claude num sandbox por sessão, perguntam ao humano
    no Linear e corrigem testes vermelhos. Não é uma workflow (sem @workflow.defn): as subclasses
    declaram o signal `human_answer` e delegam a `_receive_answer`."""

    def __init__(self) -> None:
        self._plan_sha: str | None = None
        self._clarifications: list[Clarification] = []
        self._pending_question: str | None = None
        self._answer: str | None = None

    def _receive_answer(self, answer: HumanAnswer) -> None:
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

    async def _still_in(self, issue: PlanIssueInput, state: str) -> bool:
        return await workflow.execute_activity(
            "move_issue",
            MoveIssueRequest(issue.issue_id, to_state=state, from_state=state),
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

    async def _commit(
        self,
        sandbox_id: str,
        issue: PlanIssueInput,
        base_sha: str,
        message: str,
        require_changes: bool,
    ) -> None:
        await workflow.execute_activity(
            "commit_implementation",
            CommitImplementationRequest(
                sandbox_id=sandbox_id,
                identifier=issue.identifier,
                base_sha=base_sha,
                message=message,
                require_changes=require_changes,
            ),
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3),
            result_type=str,
        )

    async def _implement_session(
        self, issue: PlanIssueInput, attempt: int, fix: FixAnalysis | None = None
    ) -> TestRunResult:
        """Implementa o plano (attempt 0) ou a correção, roda os testes e empurra o resultado:
        commit final se verde, checkpoint `wip` se vermelho. Falha da sessão de correção vira
        resultado vermelho (alimenta o loop); a da implementação inicial falha o workflow."""
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
                try:
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
                except ActivityError as error:
                    failed = TestRunResult(
                        passed=False, output=f"implement_fix falhou: {failure_message(error)}"
                    )
                    # Melhor esforço: guarda o que a sessão chegou a mudar; sem isso, segue.
                    try:
                        await self._commit(
                            sandbox_id,
                            issue,
                            base_sha,
                            f"wip: tentativa {attempt} de {issue.identifier} (correção falhou)",
                            require_changes=False,
                        )
                    except ActivityError as commit_error:
                        workflow.logger.warning(
                            "%s: checkpoint da correção que falhou não foi empurrado: %s",
                            issue.identifier,
                            failure_message(commit_error),
                        )
                    return failed
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
            await self._commit(
                sandbox_id,
                issue,
                base_sha,
                message,
                # Exige mudança só no verde direto sobre o plano: depois de um checkpoint,
                # uma correção sem mudança ainda vale.
                require_changes=tests.passed and base_sha == self._plan_sha,
            )
            return tests
        finally:
            await self._kill_sandbox(sandbox_id)

    async def _analysis_session(
        self, issue: PlanIssueInput, test_output: str, attempts: list[FixAttempt]
    ) -> FixAnalysis | str:
        """Análise da falha; se a sessão do Claude falhar, devolve a mensagem (str) para o loop
        seguir. Falhas de sandbox/clone ainda falham o workflow."""
        sandbox_id = await self._create_sandbox(issue)
        try:
            await self._clone_branch(sandbox_id, issue)
            try:
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
            except ActivityError as error:
                return f"análise falhou: {failure_message(error)}"
        finally:
            await self._kill_sandbox(sandbox_id)

    async def _ask(
        self, issue: PlanIssueInput, question: str, state: str, label: str | None = None
    ) -> bool:
        """Pergunta no Linear e espera o reply na thread. False = issue saiu de `state`.
        `label` (padrão: a própria pergunta) é o que fica nas clarificações dos prompts."""
        comment_id = str(workflow.uuid4())
        # Antes do post: um reply rápido já encontra a pergunta pendente.
        self._pending_question = comment_id
        self._answer = None
        posted_id: str = await workflow.execute_activity(
            "post_comment",
            CommentRequest(issue.issue_id, comment_id, format_question(question, state)),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            result_type=str,
        )
        # O id que vale é o do Linear; um reply que já chegou pelo id gerado não se perde.
        if posted_id and posted_id != comment_id:
            self._pending_question = posted_id
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
                if not await self._still_in(issue, state):
                    self._pending_question = None
                    return False
        self._clarifications.append(Clarification(label or question, self._answer))
        self._pending_question = None
        return True

    async def _fix_until_green(
        self, issue: PlanIssueInput, tests: TestRunResult, state: str
    ) -> TestRunResult | None:
        """Loop de testes vermelhos: analisar → planejar → corrigir → testar até ficar verde.
        Devolve o resultado verde, ou None se a issue saiu de `state` (parou)."""
        attempts: list[FixAttempt] = []
        round_start = 0
        while not tests.passed:
            if not await self._still_in(issue, state):
                workflow.logger.info("%s saiu de %s; para a correção", issue.identifier, state)
                return None
            if len(attempts) - round_start >= MAX_FIX_ATTEMPTS:
                n = len(attempts) - round_start
                question = stuck_question(n, attempts[-1], tests.output)
                if not await self._ask(issue, question, state, label=STUCK_QUESTION.format(n=n)):
                    return None
                round_start = len(attempts)
            analysis = await self._analysis_session(
                issue, tests.output, attempts[-ATTEMPTS_IN_PROMPT:]
            )
            if isinstance(analysis, str):
                # Sessão de análise falhou: conta tentativa (trava de custo) e reanalisa.
                attempts.append(
                    FixAttempt(len(attempts) + 1, analysis[:ANALYSIS_CAP], tests.output)
                )
                continue
            if analysis.question:
                if not await self._ask(issue, analysis.question, state):
                    return None
                # Reanalisa com a resposta; pergunta não conta tentativa.
                continue
            tests = await self._implement_session(issue, attempt=len(attempts) + 1, fix=analysis)
            attempts.append(
                FixAttempt(len(attempts) + 1, analysis.analysis[:ANALYSIS_CAP], tests.output)
            )
        return tests
