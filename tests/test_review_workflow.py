import unittest
import uuid

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.models import (
    AnalyzeRequest,
    CloneBranchRequest,
    CommentRequest,
    CommitImplementationRequest,
    CreateIssueRequest,
    FixAnalysis,
    FixRequest,
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
    TestRunResult,
    UsageReportRequest,
)
from software_factory.orchestration.review_workflow import (
    FOLLOW_UP_TITLE_CAP,
    MAX_REVIEW_ROUNDS,
    ReviewIssueWorkflow,
    finding_key,
    format_review_summary,
)

ISSUE = PlanIssueInput(
    issue_id="issue-1",
    identifier="ENG-12",
    title="Adicionar login",
    description=None,
    url="https://linear.app/acme/issue/ENG-12",
)
PR_URL = "https://github.com/acme/app/pull/7"
PULL = PullInfo(
    number=7, url=PR_URL, head_sha="head-1", base_ref="main", base_sha="a" * 40
)


def finding(decision: str, title: str = "achado", line: int | None = 3) -> ReviewFinding:
    return ReviewFinding(
        lens="correctness",
        severity="important",
        file="src/app.py",
        line=line,
        title=title,
        detail=f"detalhe de {title}",
        decision=decision,
        reason=f"motivo de {title}",
    )


APPROVED = ReviewReport("Tudo certo.", (finding("note"), finding("reject", "falso")))


class FakeReviewActivities:
    def __init__(
        self,
        reports: list[ReviewReport] | None = None,
        merges: list[MergeResult] | None = None,
        red_test_runs: tuple[int, ...] = (),
        answers: list[str] | None = None,
        stop_after_checks: int | None = None,
        pull_missing: bool = False,
        fail_review_times: int = 0,
        not_in_review: bool = False,
        fixed_head: str | None = None,
        fail_upsert: bool = False,
    ) -> None:
        self.fail_upsert = fail_upsert
        # Head clonado sempre igual (ninguém empurrou entre as sessões).
        self.fixed_head = fixed_head
        self.not_in_review = not_in_review
        # Relatório de cada rodada; esgotados, a review aprova.
        self.reports = list(reports or [])
        self.merges = list(merges or [])
        # Execuções de run_tests (1, 2, ...) que dão vermelho; as demais dão verde.
        self.red_test_runs = red_test_runs
        self.answers = list(answers or [])
        # Checagens de In Review depois da inicial que voltam True antes de voltar False.
        self.stop_after_checks = stop_after_checks
        self.pull_missing = pull_missing
        self.fail_review_times = fail_review_times
        self.client: Client | None = None
        self.calls: list[str] = []
        self.moves: list[MoveIssueRequest] = []
        self.commits: list[CommitImplementationRequest] = []
        self.review_requests: list[ReviewRequest] = []
        self.fix_requests: list[ReviewFixRequest] = []
        self.issue_requests: list[CreateIssueRequest] = []
        self.merge_requests: list[MergeRequest] = []
        self.comments: list[CommentRequest] = []
        self.questions: list[CommentRequest] = []
        self.review_checks = 0
        self.clones = 0
        self.test_runs = 0

    @activity.defn(name="move_issue")
    async def move_issue(self, request: MoveIssueRequest) -> bool:
        self.calls.append(f"move_issue:{request.to_state}")
        self.moves.append(request)
        if request.from_state != "In Review" or request.to_state != "In Review":
            return True
        if self.not_in_review:
            return False
        self.review_checks += 1
        if self.stop_after_checks is None or self.review_checks == 1:
            return True
        return self.review_checks - 1 <= self.stop_after_checks

    @activity.defn(name="pull_request_info")
    async def pull_request_info(self, identifier: str) -> PullInfo | None:
        self.calls.append("pull_request_info")
        return None if self.pull_missing else PULL

    @activity.defn(name="create_sandbox")
    async def create_sandbox(self, issue_id: str) -> str:
        self.calls.append("create_sandbox")
        return "sbx-1"

    @activity.defn(name="clone_branch")
    async def clone_branch(self, request: CloneBranchRequest) -> str:
        self.calls.append("clone_branch")
        self.clones += 1
        return self.fixed_head or f"sha-{self.clones}"

    @activity.defn(name="review_pull")
    async def review_pull(self, request: ReviewRequest) -> ReviewReport:
        self.calls.append("review_pull")
        self.review_requests.append(request)
        if self.fail_review_times > 0:
            self.fail_review_times -= 1
            raise ApplicationError("review.json inválido", non_retryable=True)
        if self.reports:
            return self.reports.pop(0)
        return APPROVED

    @activity.defn(name="implement_review_fix")
    async def implement_review_fix(self, request: ReviewFixRequest) -> str:
        self.calls.append("implement_review_fix")
        self.fix_requests.append(request)
        return f"corrigi a rodada {request.round}"

    @activity.defn(name="create_issue")
    async def create_issue(self, request: CreateIssueRequest) -> FollowUpIssue:
        self.calls.append("create_issue")
        self.issue_requests.append(request)
        n = len(self.issue_requests) + 100
        return FollowUpIssue(f"ENG-{n}", f"https://linear.app/acme/issue/ENG-{n}")

    @activity.defn(name="merge_pull_request")
    async def merge_pull_request(self, request: MergeRequest) -> MergeResult:
        self.calls.append("merge_pull_request")
        self.merge_requests.append(request)
        if self.merges:
            return self.merges.pop(0)
        return MergeResult(True)

    @activity.defn(name="analyze_failure")
    async def analyze_failure(self, request: AnalyzeRequest) -> FixAnalysis:
        self.calls.append("analyze_failure")
        return FixAnalysis("causa", plan="plano")

    @activity.defn(name="implement_fix")
    async def implement_fix(self, request: FixRequest) -> str:
        self.calls.append("implement_fix")
        return "Corrigido."

    @activity.defn(name="post_comment")
    async def post_comment(self, request: CommentRequest) -> str:
        self.calls.append("post_comment")
        self.comments.append(request)
        if "precisa de uma decisão" in request.body:
            self.questions.append(request)
            if self.answers:
                assert self.client is not None
                handle = self.client.get_workflow_handle(activity.info().workflow_id)
                # Sinal aguardado antes de a activity terminar: sem corrida com o salto de tempo.
                await handle.signal(
                    ReviewIssueWorkflow.human_answer,
                    HumanAnswer(request.comment_id, "reply-1", self.answers.pop(0)),
                )
        return request.comment_id

    @activity.defn(name="run_tests")
    async def run_tests(self, sandbox_id: str) -> TestRunResult:
        self.calls.append("run_tests")
        self.test_runs += 1
        return TestRunResult(
            passed=self.test_runs not in self.red_test_runs, output="FAILED test_a"
        )

    @activity.defn(name="commit_implementation")
    async def commit_implementation(self, request: CommitImplementationRequest) -> str:
        self.calls.append("commit_implementation")
        self.commits.append(request)
        return f"factory/{request.identifier}"

    @activity.defn(name="kill_sandbox")
    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.calls.append("kill_sandbox")

    @activity.defn(name="token_usage_report")
    async def token_usage_report(self, request: UsageReportRequest) -> str:
        self.calls.append(f"token_usage_report:final={request.final}")
        return "relatório"

    @activity.defn(name="upsert_comment")
    async def upsert_comment(self, request: CommentRequest) -> str:
        self.calls.append("upsert_comment")
        if self.fail_upsert:
            raise ApplicationError("linear fora", non_retryable=True)
        return request.comment_id


class TestReviewIssueWorkflow(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, fake: FakeReviewActivities) -> ReviewResult:
        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            fake.client = env.client
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ReviewIssueWorkflow],
                activities=[
                    fake.move_issue,
                    fake.pull_request_info,
                    fake.create_sandbox,
                    fake.clone_branch,
                    fake.review_pull,
                    fake.implement_review_fix,
                    fake.create_issue,
                    fake.merge_pull_request,
                    fake.analyze_failure,
                    fake.implement_fix,
                    fake.post_comment,
                    fake.run_tests,
                    fake.commit_implementation,
                    fake.kill_sandbox,
                    fake.token_usage_report,
                    fake.upsert_comment,
                ],
            ):
                return await env.client.execute_workflow(
                    ReviewIssueWorkflow.run,
                    ISSUE,
                    id=f"review-issue-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    def count(self, fake: FakeReviewActivities, name: str) -> int:
        return fake.calls.count(name)

    async def test_approved_review_merges_and_moves_to_done(self) -> None:
        fake = FakeReviewActivities()
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReviewResult("ENG-12", "merged", PR_URL))
        self.assertEqual(
            fake.calls,
            [
                "move_issue:In Review",
                "move_issue:In Review",
                "pull_request_info",
                "create_sandbox",
                "clone_branch",
                "run_tests",
                "review_pull",
                "kill_sandbox",
                "post_comment",
                "merge_pull_request",
                "move_issue:Done",
                "token_usage_report:final=True",
                "upsert_comment",
            ],
        )
        self.assertEqual(fake.moves[0].require_states, ("Done",))
        self.assertEqual(fake.moves[0].from_state, "In Review")
        self.assertEqual(fake.moves[-1].from_state, "In Review")
        self.assertEqual(fake.merge_requests, [MergeRequest("ENG-12", ISSUE.title, 7, "sha-1")])
        self.assertEqual(fake.review_requests[0].base_sha, PULL.base_sha)

    async def test_publishes_final_after_done(self) -> None:
        fake = FakeReviewActivities()
        await self.run_workflow(fake)
        self.assertEqual(
            fake.calls[-3:],
            ["move_issue:Done", "token_usage_report:final=True", "upsert_comment"],
        )
        self.assertNotIn("token_usage_report:final=False", fake.calls)

    async def test_publishes_when_stopped(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Bug.", (finding("fix", "bug"),))], stop_after_checks=1
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(
            fake.calls[-2:], ["token_usage_report:final=False", "upsert_comment"]
        )
        self.assertNotIn("token_usage_report:final=True", fake.calls)

    async def test_publish_failure_does_not_fail_workflow(self) -> None:
        fake = FakeReviewActivities(fail_upsert=True)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReviewResult("ENG-12", "merged", PR_URL))
        self.assertEqual(fake.moves[-1].to_state, "Done")

    async def test_fix_findings_trigger_fix_session_and_rereview(self) -> None:
        fix = finding("fix", "bug")
        fake = FakeReviewActivities(
            reports=[ReviewReport("Um bug.", (fix, finding("note", "nota")))]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.fix_requests), 1)
        self.assertEqual(fake.fix_requests[0].findings, (fix,))
        self.assertEqual(fake.fix_requests[0].round, 1)
        self.assertEqual([c.message for c in fake.commits], ["fix: review 1 de ENG-12"])
        self.assertFalse(fake.commits[0].require_changes)
        self.assertEqual(self.count(fake, "review_pull"), 2)
        history = fake.review_requests[1].history
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].number, 1)
        self.assertEqual(history[0].fix_notes, "corrigi a rodada 1")
        # O merge usa o sha da segunda review (sha-1 é a review, sha-2 a correção).
        self.assertEqual(fake.merge_requests[0].head_sha, "sha-3")

    async def test_red_tests_after_review_fix_enter_fix_loop(self) -> None:
        fake = FakeReviewActivities(
            # 1: antes da review (verde); 2: depois da correção da review (vermelho).
            reports=[ReviewReport("Um bug.", (finding("fix", "bug"),))], red_test_runs=(2,)
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        second_review = [i for i, c in enumerate(fake.calls) if c == "review_pull"][1]
        self.assertLess(fake.calls.index("analyze_failure"), second_review)
        self.assertLess(fake.calls.index("implement_fix"), second_review)
        self.assertEqual(
            fake.commits[0].message, "wip: review 1 de ENG-12 (testes vermelhos)"
        )

    async def test_red_head_is_fixed_before_review_and_merge(self) -> None:
        fake = FakeReviewActivities(red_test_runs=(1,))
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        reviews = [i for i, c in enumerate(fake.calls) if c == "review_pull"]
        self.assertEqual(len(reviews), 1)
        self.assertLess(fake.calls.index("run_tests"), fake.calls.index("analyze_failure"))
        self.assertLess(fake.calls.index("analyze_failure"), reviews[0])
        self.assertLess(fake.calls.index("implement_fix"), reviews[0])
        # A sessão vermelha não chega à review: testes, kill e o loop de correção.
        first_tests = fake.calls.index("run_tests")
        self.assertEqual(fake.calls[first_tests + 1], "kill_sandbox")
        history = fake.review_requests[0].history
        self.assertEqual(len(history), 1)
        self.assertIn("testes vermelhos", history[0].summary)
        # sha-1 vermelho, sha-2 análise, sha-3 correção: o merge é do sha-4, revisado e verde.
        self.assertEqual(fake.merge_requests, [MergeRequest("ENG-12", ISSUE.title, 7, "sha-4")])

    async def test_red_head_never_merges_when_stopped(self) -> None:
        fake = FakeReviewActivities(red_test_runs=(1, 2, 3), stop_after_checks=1)
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "stopped")
        self.assertNotIn("review_pull", fake.calls)
        self.assertNotIn("merge_pull_request", fake.calls)

    async def test_issue_findings_create_follow_up_issues_once(self) -> None:
        fake = FakeReviewActivities(
            reports=[
                ReviewReport("Dois follow-ups.", (finding("issue", "a"), finding("issue", "b")))
            ]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.issue_requests), 2)
        ids = {r.new_issue_id for r in fake.issue_requests}
        self.assertEqual(len(ids), 2)
        self.assertEqual(fake.issue_requests[0].origin_issue_id, "issue-1")
        self.assertIn("a", fake.issue_requests[0].title)
        self.assertIn(ISSUE.url, fake.issue_requests[0].description)
        self.assertIn("src/app.py:3", fake.issue_requests[0].description)
        self.assertIn("detalhe de a", fake.issue_requests[0].description)
        self.assertEqual(result.follow_ups, ("ENG-101", "ENG-102"))
        summary = fake.comments[0].body
        self.assertIn("https://linear.app/acme/issue/ENG-101", summary)
        self.assertIn("https://linear.app/acme/issue/ENG-102", summary)
        self.assertEqual(self.count(fake, "create_issue"), 2)

    async def test_issue_finding_repeated_in_later_round_is_not_recreated(self) -> None:
        follow = finding("issue", "fora do escopo")
        fake = FakeReviewActivities(
            reports=[
                ReviewReport("Follow-up e bug.", (follow, finding("fix", "bug"))),
                ReviewReport("Só o follow-up.", (follow,)),
            ]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(self.count(fake, "create_issue"), 1)
        self.assertEqual(result.follow_ups, ("ENG-101",))
        # O resumo da segunda rodada ainda leva o link da issue já criada.
        self.assertIn("https://linear.app/acme/issue/ENG-101", fake.comments[1].body)

    async def test_issue_finding_on_another_line_in_later_round_is_not_recreated(self) -> None:
        fake = FakeReviewActivities(
            reports=[
                ReviewReport(
                    "Follow-up e bug.", (finding("issue", "fora do escopo"), finding("fix", "bug"))
                ),
                # A correção deslocou o código: mesmo achado, outra linha.
                ReviewReport("Só o follow-up.", (finding("issue", "fora do escopo", line=9),)),
            ]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(self.count(fake, "create_issue"), 1)
        self.assertEqual(result.follow_ups, ("ENG-101",))

    async def test_follow_up_title_is_capped(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Título longo.", (finding("issue", "x" * 500),))]
        )
        await self.run_workflow(fake)
        self.assertEqual(len(fake.issue_requests[0].title), FOLLOW_UP_TITLE_CAP)
        self.assertTrue(fake.issue_requests[0].title.startswith("[ENG-12] xxx"))

    async def test_ask_finding_waits_for_answer_and_rereviews_with_clarification(self) -> None:
        fake = FakeReviewActivities(
            reports=[
                ReviewReport(
                    "Dúvida e bug.", (finding("ask", "qual api?"), finding("fix", "bug"))
                )
            ],
            answers=["use a v2"],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.questions), 1)
        self.assertIn("detalhe de qual api?", fake.questions[0].body)
        first_comments = [i for i, c in enumerate(fake.calls) if c == "post_comment"][:2]
        second_review = [i for i, c in enumerate(fake.calls) if c == "review_pull"][1]
        self.assertLess(first_comments[1], second_review)
        self.assertNotIn("implement_review_fix", fake.calls)
        clarifications = fake.review_requests[1].clarifications
        self.assertEqual(len(clarifications), 1)
        self.assertEqual(clarifications[0].answer, "use a v2")

    async def test_several_asks_join_into_one_question(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Dúvidas.", (finding("ask", "p1"), finding("ask", "p2")))],
            answers=["ok"],
        )
        await self.run_workflow(fake)
        self.assertEqual(len(fake.questions), 1)
        self.assertIn("detalhe de p1", fake.questions[0].body)
        self.assertIn("detalhe de p2", fake.questions[0].body)

    async def test_stuck_after_max_review_rounds_asks_human(self) -> None:
        fix_report = ReviewReport("Bug de novo.", (finding("fix", "bug"),))
        fake = FakeReviewActivities(
            reports=[fix_report] * (MAX_REVIEW_ROUNDS + 1), answers=["siga"]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.questions), 1)
        reviews = [i for i, c in enumerate(fake.calls) if c == "review_pull"]
        question_at = [
            i for i, c in enumerate(fake.calls) if c == "post_comment"
        ][MAX_REVIEW_ROUNDS]
        self.assertLess(reviews[MAX_REVIEW_ROUNDS - 1], question_at)
        self.assertLess(question_at, reviews[MAX_REVIEW_ROUNDS])
        # A resposta zera o contador: mais uma rodada de fix sem nova pergunta.
        self.assertEqual(len(fake.fix_requests), MAX_REVIEW_ROUNDS + 1)
        self.assertEqual(fake.review_requests[MAX_REVIEW_ROUNDS].clarifications[0].answer, "siga")

    async def test_moved_out_of_in_review_stops(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Bug.", (finding("fix", "bug"),))], stop_after_checks=1
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReviewResult("ENG-12", "stopped"))
        self.assertNotIn("merge_pull_request", fake.calls)

    async def test_skips_when_not_in_review(self) -> None:
        fake = FakeReviewActivities(not_in_review=True)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReviewResult("ENG-12", "skipped"))
        self.assertEqual(fake.calls, ["move_issue:In Review"])

    async def test_no_open_pull_request_is_no_pr(self) -> None:
        fake = FakeReviewActivities(pull_missing=True)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ReviewResult("ENG-12", "no_pr"))
        self.assertNotIn("create_sandbox", fake.calls)

    async def test_no_pr_publishes_token_usage(self) -> None:
        fake = FakeReviewActivities(pull_missing=True)
        await self.run_workflow(fake)
        self.assertEqual(
            fake.calls[-2:], ["token_usage_report:final=False", "upsert_comment"]
        )

    async def test_merge_blocked_asks_human_then_retries(self) -> None:
        fake = FakeReviewActivities(
            merges=[MergeResult(False, reason="não mergeável (dirty)"), MergeResult(True)],
            answers=["resolvi o conflito"],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.questions), 1)
        self.assertIn("não mergeável (dirty)", fake.questions[0].body)
        self.assertIn(PR_URL, fake.questions[0].body)
        self.assertEqual(self.count(fake, "review_pull"), 2)
        self.assertEqual(self.count(fake, "merge_pull_request"), 2)
        self.assertLess(
            fake.calls.index("merge_pull_request"),
            [i for i, c in enumerate(fake.calls) if c == "review_pull"][1],
        )

    async def test_sensitive_files_ask_confirmation_before_merge(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Tudo certo.", (), sensitive_files=("CLAUDE.md", "Makefile"))],
            answers=["pode mergear"],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.questions), 1)
        body = fake.questions[0].body
        self.assertIn("CLAUDE.md", body)
        self.assertIn("Makefile", body)
        self.assertIn("sha-1", body)
        question_at = fake.calls.index("post_comment") + 1
        self.assertEqual(fake.calls[question_at], "post_comment")
        self.assertLess(question_at, fake.calls.index("merge_pull_request"))
        self.assertEqual(fake.merge_requests, [MergeRequest("ENG-12", ISSUE.title, 7, "sha-1")])

    async def test_sensitive_files_confirmed_sha_is_not_asked_again(self) -> None:
        sensitive = ReviewReport("Tudo certo.", (), sensitive_files=("CLAUDE.md",))
        fake = FakeReviewActivities(
            reports=[sensitive, sensitive],
            merges=[MergeResult(False, reason="não mergeável (dirty)"), MergeResult(True)],
            answers=["confirmo", "resolvi"],
            fixed_head="sha-x",
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        # Confirmação do sha-x + merge bloqueado; a segunda review do mesmo sha não pergunta.
        self.assertEqual(len(fake.questions), 2)
        self.assertIn("CLAUDE.md", fake.questions[0].body)
        self.assertIn("não mergeável", fake.questions[1].body)

    async def test_stopped_while_confirming_sensitive_files_does_not_merge(self) -> None:
        fake = FakeReviewActivities(
            reports=[ReviewReport("Tudo certo.", (), sensitive_files=("CLAUDE.md",))],
            stop_after_checks=1,
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(len(fake.questions), 1)
        self.assertNotIn("merge_pull_request", fake.calls)

    async def test_head_changed_since_review_rereviews(self) -> None:
        fake = FakeReviewActivities(merges=[MergeResult(False, head_changed=True)])
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(self.count(fake, "review_pull"), 2)
        self.assertEqual(fake.questions, [])
        self.assertEqual(fake.merge_requests[1].head_sha, "sha-2")

    async def test_head_changed_every_time_hits_stuck_question(self) -> None:
        fake = FakeReviewActivities(
            merges=[MergeResult(False, head_changed=True)] * MAX_REVIEW_ROUNDS,
            answers=["siga"],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(len(fake.questions), 1)
        self.assertIn(f"{MAX_REVIEW_ROUNDS} rodadas", fake.questions[0].body)
        reviews = [i for i, c in enumerate(fake.calls) if c == "review_pull"]
        self.assertEqual(len(reviews), MAX_REVIEW_ROUNDS + 1)
        question_at = fake.calls.index("post_comment", reviews[MAX_REVIEW_ROUNDS - 1] + 2)
        self.assertLess(question_at, reviews[MAX_REVIEW_ROUNDS])

    async def test_review_session_failure_counts_round_and_retries(self) -> None:
        fake = FakeReviewActivities(fail_review_times=1)
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "merged")
        self.assertEqual(self.count(fake, "review_pull"), 2)
        self.assertEqual(self.count(fake, "kill_sandbox"), 2)
        history = fake.review_requests[1].history
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].summary.startswith("review falhou:"))
        self.assertEqual(self.count(fake, "merge_pull_request"), 1)


class TestFormatReviewSummary(unittest.TestCase):
    def test_lists_findings_and_follow_up_links(self) -> None:
        issue_finding = finding("issue", "follow")
        report = ReviewReport(
            "Resumo da rodada.", (finding("fix", "bug", line=None), issue_finding)
        )

        body = format_review_summary(
            2,
            report,
            {finding_key(issue_finding): FollowUpIssue("ENG-9", "https://linear.app/x/ENG-9")},
        )
        self.assertIn("2", body)
        self.assertIn("Resumo da rodada.", body)
        self.assertIn("`src/app.py`", body)
        self.assertIn("`src/app.py:3`", body)
        self.assertIn("motivo de bug", body)
        self.assertIn("correctness", body)
        self.assertIn("important", body)
        self.assertIn("[ENG-9](https://linear.app/x/ENG-9)", body)


if __name__ == "__main__":
    unittest.main()
