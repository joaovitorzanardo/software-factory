from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from software_factory.orchestration.models import (
        OUTCOME_MERGED,
        OUTCOME_NO_PR,
        OUTCOME_SKIPPED,
        OUTCOME_STOPPED,
        STATE_DONE,
        STATE_IN_REVIEW,
        CommentRequest,
        CreateIssueRequest,
        FollowUpIssue,
        HumanAnswer,
        MergeRequest,
        MergeResult,
        MoveIssueRequest,
        PlanIssueInput,
        PullInfo,
        ReviewFinding,
        ReviewFixRequest,
        ReviewReport,
        ReviewRequest,
        ReviewResult,
        ReviewRound,
        TestRunResult,
    )
    from software_factory.orchestration.sessions import SessionWorkflow, failure_message

# Rodadas seguidas (correção ou review que falhou) sem aprovação antes de perguntar no Linear.
MAX_REVIEW_ROUNDS = 3
# Rótulo curto da pergunta de travamento nas clarificações (o comentário leva o texto inteiro).
REVIEW_STUCK_QUESTION = (
    "Fiz {n} rodadas de review e correção sem aprovação. Como devo seguir?"
)
# Título da issue de follow-up (o título do achado vem do LLM, sem limite).
FOLLOW_UP_TITLE_CAP = 200
QUICK_CALL_TIMEOUT = timedelta(seconds=30)
QUICK_CALL_RETRY = RetryPolicy(maximum_attempts=5)
# mergeable=None (GitHub ainda calculando) vira erro retentável: espera entre tentativas.
MERGE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=5)


def finding_where(f: ReviewFinding) -> str:
    return f"{f.file}:{f.line}" if f.line is not None else f.file


def finding_key(f: ReviewFinding) -> str:
    """Chave do achado em `follow_ups`; achados iguais (na rodada ou em rodadas seguintes)
    viram uma issue só. Sem a linha: uma correção que desloca o código não duplica a issue."""
    return f"{f.lens}:{f.file}:{f.title}"


def format_review_summary(
    round: int, report: ReviewReport, follow_ups: dict[str, FollowUpIssue]
) -> str:
    lines = [f"**software-factory: review {round}**", "", report.summary, ""]
    if not report.findings:
        lines.append("- (sem achados)")
    for f in report.findings:
        line = (
            f"- [{f.lens}/{f.severity}] `{f.decision}` `{finding_where(f)}`: "
            f"{f.title} ({f.reason})"
        )
        follow_up = follow_ups.get(finding_key(f))
        if follow_up is not None:
            line += f" → [{follow_up.identifier}]({follow_up.url})"
        lines.append(line)
    if follow_ups:
        links = ", ".join(f"[{i.identifier}]({i.url})" for i in follow_ups.values())
        lines += ["", f"Issues de follow-up: {links}"]
    return "\n".join(lines)


def follow_up_description(issue: PlanIssueInput, f: ReviewFinding) -> str:
    origin = f"[{issue.identifier}]({issue.url})" if issue.url else issue.identifier
    return (
        f"Achado da review automática da PR de {origin} "
        f"({f.lens}/{f.severity}), em `{finding_where(f)}`.\n\n"
        f"{f.detail}\n\n"
        f"**Por que virou issue:** {f.reason}\n\n"
        f"_Criada pela review automática de {issue.identifier}. "
        "Adicione a label Feature ou Bug para a software-factory assumir esta issue._"
    )


def asks_question(findings: list[ReviewFinding]) -> str:
    items = "\n\n".join(
        f"**{i}. {f.title}** (`{finding_where(f)}`, {f.lens}/{f.severity})\n\n{f.detail}"
        for i, f in enumerate(findings, start=1)
    )
    return f"A review da PR levantou {len(findings)} ponto(s) que precisam de decisão:\n\n{items}"


def sensitive_files_question(pull: PullInfo, sha: str, files: tuple[str, ...]) -> str:
    items = "\n".join(f"- `{path}`" for path in files)
    return (
        f"A review aprovou a PR {pull.url} (sha `{sha}`), mas ela muda arquivos sensíveis, que "
        "alteram o comportamento do agente, da CI ou dos testes:\n\n"
        f"{items}\n\n"
        "Confira essas mudanças e responda nesta thread para eu fazer o merge deste sha. "
        "O merge só acontece com a sua confirmação."
    )


def review_stuck_question(n: int, last_round: ReviewRound) -> str:
    items = "\n".join(
        f"- [{f.lens}/{f.severity}] `{f.decision}` `{finding_where(f)}`: {f.title}"
        for f in last_round.findings
    )
    notes = ""
    if last_round.fix_notes:
        notes = f"\n\n**Resumo da última correção:**\n{last_round.fix_notes}"
    return (
        f"Fiz {n} rodadas de review e correção e a PR ainda não foi aprovada.\n\n"
        f"**Última review ({last_round.number}):**\n{last_round.summary}\n\n"
        f"{items or '- (sem achados)'}{notes}\n\n"
        "Como devo seguir?"
    )


@workflow.defn
class ReviewIssueWorkflow(SessionWorkflow):
    """Issue em In Review: revisa a PR de factory/<issue> num sandbox (lentes correctness e
    security + triagem). Achados `issue` viram issues de follow-up no Linear, `ask` vira
    pergunta no Linear e `fix` vira uma sessão de correção (testes vermelhos depois dela entram
    no loop de correção). Repete até a review aprovar; então faz squash merge no sha revisado e
    move a issue para Done. MAX_REVIEW_ROUNDS rodadas seguidas sem aprovação viram pergunta.
    Issue fora de In Review: para (OUTCOME_STOPPED), sem merge. Em falha, sem compensação."""

    def __init__(self) -> None:
        super().__init__()
        # O histórico já tem push: nenhuma correção da review exige mudança.
        self._plan_sha = ""

    @workflow.signal
    def human_answer(self, answer: HumanAnswer) -> None:
        self._receive_answer(answer)

    @workflow.run
    async def run(self, issue: PlanIssueInput) -> ReviewResult:
        still_in_review = await self._move(
            MoveIssueRequest(
                issue.issue_id,
                to_state=STATE_IN_REVIEW,
                from_state=STATE_IN_REVIEW,
                require_states=(STATE_DONE,),
            )
        )
        if not still_in_review:
            workflow.logger.info("%s saiu de %s; não revisa", issue.identifier, STATE_IN_REVIEW)
            return ReviewResult(issue.identifier, OUTCOME_SKIPPED)

        rounds: list[ReviewRound] = []
        fix_rounds = 0
        follow_ups: list[str] = []
        # Follow-ups já criados, entre rodadas: a re-review que repete o achado reaproveita.
        known_follow_ups: dict[str, FollowUpIssue] = {}
        # Shas com arquivos sensíveis cujo merge a pessoa já confirmou: não pergunta de novo.
        confirmed_shas: list[str] = []
        while True:
            if not await self._still_in(issue, STATE_IN_REVIEW):
                return self._stopped(issue, follow_ups)
            pull: PullInfo | None = await workflow.execute_activity(
                "pull_request_info",
                issue.identifier,
                start_to_close_timeout=QUICK_CALL_TIMEOUT,
                retry_policy=QUICK_CALL_RETRY,
                result_type=PullInfo,
            )
            if pull is None:
                workflow.logger.info("%s sem PR aberta", issue.identifier)
                return ReviewResult(issue.identifier, OUTCOME_NO_PR, follow_ups=tuple(follow_ups))
            if fix_rounds >= MAX_REVIEW_ROUNDS:
                question = review_stuck_question(fix_rounds, rounds[-1])
                label = REVIEW_STUCK_QUESTION.format(n=fix_rounds)
                if not await self._ask(issue, question, STATE_IN_REVIEW, label=label):
                    return self._stopped(issue, follow_ups)
                fix_rounds = 0

            number = len(rounds) + 1
            reviewed = await self._review_session(issue, pull, rounds)
            if isinstance(reviewed, str):
                # Sessão de review falhou: conta rodada (trava de custo) e revisa de novo.
                rounds.append(ReviewRound(number, f"review falhou: {reviewed}"))
                fix_rounds += 1
                continue
            report, reviewed_sha = reviewed
            review_round = ReviewRound(number, report.summary, report.findings)

            created = await self._create_follow_ups(issue, report, known_follow_ups)
            follow_ups += [
                i.identifier for i in created.values() if i.identifier not in follow_ups
            ]
            await workflow.execute_activity(
                "post_comment",
                CommentRequest(
                    issue.issue_id,
                    str(workflow.uuid4()),
                    format_review_summary(number, report, created),
                ),
                start_to_close_timeout=QUICK_CALL_TIMEOUT,
                retry_policy=QUICK_CALL_RETRY,
                result_type=str,
            )

            asks = [f for f in report.findings if f.decision == "ask"]
            fixes = [f for f in report.findings if f.decision == "fix"]
            if asks:
                label = f"Review {number}: " + "; ".join(f.title for f in asks)
                if not await self._ask(issue, asks_question(asks), STATE_IN_REVIEW, label=label):
                    return self._stopped(issue, follow_ups)
                # Não corrige antes da resposta: a próxima review já a leva em conta.
                rounds.append(review_round)
                continue
            if fixes:
                tests, notes = await self._review_fix_session(issue, number, fixes)
                if not tests.passed:
                    green = await self._fix_until_green(issue, tests, STATE_IN_REVIEW)
                    if green is None:
                        return self._stopped(issue, follow_ups)
                rounds.append(
                    ReviewRound(number, report.summary, report.findings, fix_notes=notes)
                )
                fix_rounds += 1
                continue

            rounds.append(review_round)
            if report.sensitive_files and reviewed_sha not in confirmed_shas:
                label = (
                    f"Confirmar o merge de {reviewed_sha[:12]} com "
                    f"{len(report.sensitive_files)} arquivo(s) sensível(is)"
                )
                question = sensitive_files_question(pull, reviewed_sha, report.sensitive_files)
                if not await self._ask(issue, question, STATE_IN_REVIEW, label=label):
                    return self._stopped(issue, follow_ups)
                # A resposta confirma o merge deste sha; para parar, a pessoa move a issue.
                confirmed_shas.append(reviewed_sha)
            merge: MergeResult = await workflow.execute_activity(
                "merge_pull_request",
                MergeRequest(issue.identifier, issue.title, pull.number, reviewed_sha),
                start_to_close_timeout=QUICK_CALL_TIMEOUT,
                retry_policy=MERGE_RETRY,
                result_type=MergeResult,
            )
            if merge.merged:
                await self._move(
                    MoveIssueRequest(
                        issue.issue_id, to_state=STATE_DONE, from_state=STATE_IN_REVIEW
                    )
                )
                return ReviewResult(
                    issue.identifier, OUTCOME_MERGED, pull.url, tuple(follow_ups)
                )
            if merge.head_changed:
                # Alguém empurrou depois da review: revisa o head novo.
                continue
            question = (
                f"A PR {pull.url} não pode ser mergeada: {merge.reason}. "
                "Resolva no GitHub e responda nesta thread para eu revisar de novo e tentar o "
                "merge, ou diga como seguir."
            )
            label = f"Merge bloqueado: {merge.reason}"
            if not await self._ask(issue, question, STATE_IN_REVIEW, label=label):
                return self._stopped(issue, follow_ups)

    def _stopped(self, issue: PlanIssueInput, follow_ups: list[str]) -> ReviewResult:
        workflow.logger.info("%s saiu de %s; para a review", issue.identifier, STATE_IN_REVIEW)
        return ReviewResult(issue.identifier, OUTCOME_STOPPED, follow_ups=tuple(follow_ups))

    async def _create_follow_ups(
        self, issue: PlanIssueInput, report: ReviewReport, known: dict[str, FollowUpIssue]
    ) -> dict[str, FollowUpIssue]:
        """Follow-ups dos achados `issue` da rodada. Cria só as chaves que não estão em `known`
        (que é atualizado); as já vistas reaproveitam a issue existente."""
        created: dict[str, FollowUpIssue] = {}
        for f in report.findings:
            key = finding_key(f)
            if f.decision != "issue" or key in created:
                continue
            if key in known:
                created[key] = known[key]
                continue
            created[key] = known[key] = await workflow.execute_activity(
                "create_issue",
                CreateIssueRequest(
                    origin_issue_id=issue.issue_id,
                    # Determinístico no replay: o retry da activity reaproveita a issue criada.
                    new_issue_id=str(workflow.uuid4()),
                    title=f"[{issue.identifier}] {f.title}"[:FOLLOW_UP_TITLE_CAP],
                    description=follow_up_description(issue, f),
                ),
                start_to_close_timeout=QUICK_CALL_TIMEOUT,
                retry_policy=QUICK_CALL_RETRY,
                result_type=FollowUpIssue,
            )
        return created

    async def _review_session(
        self, issue: PlanIssueInput, pull: PullInfo, rounds: list[ReviewRound]
    ) -> tuple[ReviewReport, str] | str:
        """Review da PR no head clonado; devolve o relatório e o sha revisado, ou a mensagem de
        falha da sessão do Claude. Falhas de sandbox/clone ainda falham o workflow."""
        sandbox_id = await self._create_sandbox(issue)
        try:
            reviewed_sha = await self._clone_branch(sandbox_id, issue)
            try:
                report: ReviewReport = await workflow.execute_activity(
                    "review_pull",
                    ReviewRequest(
                        sandbox_id=sandbox_id,
                        issue=issue,
                        base_sha=pull.base_sha,
                        history=tuple(rounds),
                        clarifications=tuple(self._clarifications),
                    ),
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    result_type=ReviewReport,
                )
            except ActivityError as error:
                return failure_message(error)
            return report, reviewed_sha
        finally:
            await self._kill_sandbox(sandbox_id)

    async def _review_fix_session(
        self, issue: PlanIssueInput, round: int, findings: list[ReviewFinding]
    ) -> tuple[TestRunResult, str]:
        """Corrige os achados `fix` da rodada, roda os testes e empurra o resultado. Falha da
        sessão do Claude vira vermelho (alimenta o loop) com checkpoint de melhor esforço."""
        sandbox_id = await self._create_sandbox(issue)
        try:
            base_sha = await self._clone_branch(sandbox_id, issue)
            try:
                notes: str = await workflow.execute_activity(
                    "implement_review_fix",
                    ReviewFixRequest(
                        sandbox_id=sandbox_id,
                        issue=issue,
                        round=round,
                        findings=tuple(findings),
                        clarifications=tuple(self._clarifications),
                    ),
                    start_to_close_timeout=timedelta(minutes=35),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    result_type=str,
                )
            except ActivityError as error:
                message = f"implement_review_fix falhou: {failure_message(error)}"
                try:
                    await self._commit(
                        sandbox_id,
                        issue,
                        base_sha,
                        f"wip: review {round} de {issue.identifier} (correção falhou)",
                        require_changes=False,
                    )
                except ActivityError as commit_error:
                    workflow.logger.warning(
                        "%s: checkpoint da correção da review que falhou não foi empurrado: %s",
                        issue.identifier,
                        failure_message(commit_error),
                    )
                return TestRunResult(passed=False, output=message), message
            tests: TestRunResult = await workflow.execute_activity(
                "run_tests",
                sandbox_id,
                start_to_close_timeout=timedelta(minutes=12),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
                result_type=TestRunResult,
            )
            if tests.passed:
                message = f"fix: review {round} de {issue.identifier}"
            else:
                message = f"wip: review {round} de {issue.identifier} (testes vermelhos)"
            await self._commit(sandbox_id, issue, base_sha, message, require_changes=False)
            return tests, notes
        finally:
            await self._kill_sandbox(sandbox_id)
