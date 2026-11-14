import unittest
import uuid

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import (
    AnalyzeRequest,
    Clarification,
    CloneBranchRequest,
    CommentRequest,
    CommitImplementationRequest,
    FixAnalysis,
    FixRequest,
    HumanAnswer,
    ImplementRequest,
    ImplementResult,
    MoveIssueRequest,
    PlanIssueInput,
    PullRequestRequest,
    TestRunResult,
)

ISSUE = PlanIssueInput(
    issue_id="issue-1",
    identifier="ENG-12",
    title="Adicionar login",
    description=None,
    url="https://linear.app/acme/issue/ENG-12",
)
PR_URL = "https://github.com/acme/app/pull/7"


class FakeActivities:
    def __init__(
        self,
        not_in_progress: bool = False,
        no_plan: bool = False,
        red_runs: int = 0,
        fail_implement: bool = False,
        fail_commit: bool = False,
        analyses: list[FixAnalysis] | None = None,
        answers: list[str] | None = None,
        stop_after_checks: int | None = None,
        wrong_answer_first: str | None = None,
    ) -> None:
        self.not_in_progress = not_in_progress
        self.no_plan = no_plan
        # Quantas execuções de run_tests vêm vermelhas antes do verde.
        self.red_runs = red_runs
        self.fail_implement = fail_implement
        self.fail_commit = fail_commit
        self.analyses = list(analyses or [])
        self.answers = list(answers or [])
        # Checagens de In Progress depois da inicial que voltam True antes de voltar False.
        self.stop_after_checks = stop_after_checks
        # Antes de cada resposta certa, manda uma resposta com question_id de outra pergunta.
        self.wrong_answer_first = wrong_answer_first
        self.client: Client | None = None
        self.calls: list[str] = []
        self.moves: list[MoveIssueRequest] = []
        self.commits: list[CommitImplementationRequest] = []
        self.pulls: list[PullRequestRequest] = []
        self.analyze_requests: list[AnalyzeRequest] = []
        self.fix_requests: list[FixRequest] = []
        self.comments: list[CommentRequest] = []
        self.progress_checks = 0
        self.clones = 0
        self.test_runs = 0

    @activity.defn(name="move_issue")
    async def move_issue(self, request: MoveIssueRequest) -> bool:
        self.calls.append(f"move_issue:{request.to_state}")
        self.moves.append(request)
        if request.from_state != "In Progress":
            return True
        if self.not_in_progress:
            return False
        self.progress_checks += 1
        if self.stop_after_checks is None or self.progress_checks == 1:
            return True
        return self.progress_checks - 1 <= self.stop_after_checks

    @activity.defn(name="plan_exists")
    async def plan_exists(self, identifier: str) -> bool:
        self.calls.append("plan_exists")
        return not self.no_plan

    @activity.defn(name="create_sandbox")
    async def create_sandbox(self, issue_id: str) -> str:
        self.calls.append("create_sandbox")
        return "sbx-1"

    @activity.defn(name="clone_branch")
    async def clone_branch(self, request: CloneBranchRequest) -> str:
        self.calls.append("clone_branch")
        self.clones += 1
        return f"sha-{self.clones}"

    @activity.defn(name="implement_plan")
    async def implement_plan(self, request: ImplementRequest) -> str:
        self.calls.append("implement_plan")
        if self.fail_implement:
            raise ApplicationError("claude falhou", non_retryable=True)
        return "Implementado."

    @activity.defn(name="analyze_failure")
    async def analyze_failure(self, request: AnalyzeRequest) -> FixAnalysis:
        self.calls.append("analyze_failure")
        self.analyze_requests.append(request)
        if self.analyses:
            return self.analyses.pop(0)
        return FixAnalysis("causa", plan="plano")

    @activity.defn(name="implement_fix")
    async def implement_fix(self, request: FixRequest) -> str:
        self.calls.append("implement_fix")
        self.fix_requests.append(request)
        return "Corrigido."

    @activity.defn(name="post_comment")
    async def post_comment(self, request: CommentRequest) -> str:
        self.calls.append("post_comment")
        self.comments.append(request)
        if self.answers:
            assert self.client is not None
            handle = self.client.get_workflow_handle(activity.info().workflow_id)
            # Sinal enviado (e aguardado) antes de a activity terminar: sem corrida com o
            # salto de tempo do ambiente de teste.
            if self.wrong_answer_first is not None:
                await handle.signal(
                    ImplementIssueWorkflow.human_answer,
                    HumanAnswer("outra-pergunta", "reply-0", self.wrong_answer_first),
                )
            await handle.signal(
                ImplementIssueWorkflow.human_answer,
                HumanAnswer(request.comment_id, "reply-1", self.answers.pop(0)),
            )
        return f"comment-{len(self.comments)}"

    @activity.defn(name="run_tests")
    async def run_tests(self, sandbox_id: str) -> TestRunResult:
        self.calls.append("run_tests")
        self.test_runs += 1
        return TestRunResult(passed=self.test_runs > self.red_runs, output="FAILED test_a")

    @activity.defn(name="commit_implementation")
    async def commit_implementation(self, request: CommitImplementationRequest) -> str:
        self.calls.append("commit_implementation")
        self.commits.append(request)
        if self.fail_commit:
            raise ApplicationError("push falhou", non_retryable=True)
        return f"factory/{request.identifier}"

    @activity.defn(name="kill_sandbox")
    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.calls.append("kill_sandbox")

    @activity.defn(name="open_pull_request")
    async def open_pull_request(self, request: PullRequestRequest) -> str:
        self.calls.append("open_pull_request")
        self.pulls.append(request)
        return PR_URL


class TestImplementIssueWorkflow(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, fake: FakeActivities) -> ImplementResult:
        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            fake.client = env.client
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ImplementIssueWorkflow],
                activities=[
                    fake.move_issue,
                    fake.plan_exists,
                    fake.create_sandbox,
                    fake.clone_branch,
                    fake.implement_plan,
                    fake.analyze_failure,
                    fake.implement_fix,
                    fake.post_comment,
                    fake.run_tests,
                    fake.commit_implementation,
                    fake.kill_sandbox,
                    fake.open_pull_request,
                ],
            ):
                return await env.client.execute_workflow(
                    ImplementIssueWorkflow.run,
                    ISSUE,
                    id=f"implement-issue-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    async def test_happy_path(self) -> None:
        fake = FakeActivities()
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "pr_opened", PR_URL))
        self.assertEqual(
            fake.calls,
            [
                "move_issue:In Progress",
                "plan_exists",
                "create_sandbox",
                "clone_branch",
                "implement_plan",
                "run_tests",
                "commit_implementation",
                "kill_sandbox",
                "open_pull_request",
                "move_issue:In Review",
            ],
        )
        self.assertEqual(
            fake.moves[0],
            MoveIssueRequest(
                "issue-1",
                to_state="In Progress",
                from_state="In Progress",
                require_states=("In Review", "In Planning"),
            ),
        )
        self.assertEqual(fake.moves[-1], MoveIssueRequest("issue-1", to_state="In Review"))
        self.assertEqual(fake.commits[0].base_sha, "sha-1")
        self.assertEqual(fake.commits[0].message, "feat: implementa ENG-12")
        self.assertIs(fake.commits[0].require_changes, True)
        self.assertEqual(
            fake.pulls, [PullRequestRequest("ENG-12", "Adicionar login", ISSUE.url)]
        )

    async def test_skips_when_not_in_progress(self) -> None:
        fake = FakeActivities(not_in_progress=True)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "skipped"))
        self.assertEqual(fake.calls, ["move_issue:In Progress"])

    async def test_no_plan_moves_back_to_planning(self) -> None:
        fake = FakeActivities(no_plan=True)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "no_plan"))
        self.assertEqual(
            fake.calls, ["move_issue:In Progress", "plan_exists", "move_issue:In Planning"]
        )
        self.assertEqual(
            fake.moves[-1],
            MoveIssueRequest("issue-1", to_state="In Planning", from_state="In Progress"),
        )

    async def test_red_then_fixed_opens_pr(self) -> None:
        fake = FakeActivities(red_runs=1)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "pr_opened", PR_URL))
        self.assertEqual(
            fake.calls,
            [
                "move_issue:In Progress",
                "plan_exists",
                "create_sandbox",
                "clone_branch",
                "implement_plan",
                "run_tests",
                "commit_implementation",
                "kill_sandbox",
                "move_issue:In Progress",
                "create_sandbox",
                "clone_branch",
                "analyze_failure",
                "kill_sandbox",
                "create_sandbox",
                "clone_branch",
                "implement_fix",
                "run_tests",
                "commit_implementation",
                "kill_sandbox",
                "open_pull_request",
                "move_issue:In Review",
            ],
        )
        self.assertEqual(fake.commits[0].message, "wip: tentativa 0 de ENG-12 (testes vermelhos)")
        self.assertFalse(fake.commits[0].require_changes)
        self.assertEqual(fake.commits[1].message, "feat: implementa ENG-12")
        # Já houve checkpoint: a correção pode não mudar nada e ainda assim valer.
        self.assertFalse(fake.commits[1].require_changes)
        self.assertEqual(fake.analyze_requests[0].test_output, "FAILED test_a")
        self.assertEqual(fake.fix_requests[0].plan, "plano")
        self.assertEqual(fake.fix_requests[0].analysis, "causa")

    async def test_passes_previous_attempts_to_analysis(self) -> None:
        fake = FakeActivities(red_runs=5)
        await self.run_workflow(fake)
        self.assertEqual([a.number for a in fake.analyze_requests[-1].attempts], [2, 3, 4])

    async def test_question_waits_for_answer_and_reanalyzes(self) -> None:
        fake = FakeActivities(
            red_runs=1,
            answers=["Use bcrypt"],
            analyses=[
                FixAnalysis("causa", question="bcrypt ou argon2?"),
                FixAnalysis("causa", plan="plano"),
            ],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "pr_opened")
        self.assertIn("bcrypt ou argon2?", fake.comments[0].body)
        self.assertIn("nesta thread", fake.comments[0].body)
        self.assertEqual(fake.comments[0].issue_id, "issue-1")
        self.assertEqual(
            fake.analyze_requests[1].clarifications,
            (Clarification("bcrypt ou argon2?", "Use bcrypt"),),
        )
        self.assertEqual(
            fake.fix_requests[0].clarifications,
            (Clarification("bcrypt ou argon2?", "Use bcrypt"),),
        )
        # Pergunta não conta tentativa.
        self.assertEqual(fake.calls.count("implement_fix"), 1)

    async def test_ignores_answer_to_other_question(self) -> None:
        fake = FakeActivities(
            red_runs=1,
            answers=["certa"],
            wrong_answer_first="errada",
            analyses=[FixAnalysis("causa", question="?")],
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "pr_opened")
        self.assertEqual(len(fake.analyze_requests[1].clarifications), 1)
        self.assertEqual(fake.analyze_requests[1].clarifications[0].answer, "certa")

    async def test_asks_after_max_attempts(self) -> None:
        fake = FakeActivities(red_runs=6, answers=["tente mockar o relógio"])
        result = await self.run_workflow(fake)
        self.assertEqual(result.outcome, "pr_opened")
        self.assertEqual(len(fake.comments), 1)
        self.assertIn("Fiz 5 tentativas", fake.comments[0].body)
        self.assertEqual(fake.calls.count("implement_fix"), 6)
        self.assertEqual(
            fake.analyze_requests[-1].clarifications[0].answer, "tente mockar o relógio"
        )

    async def test_noop_fix_keeps_looping(self) -> None:
        fake = FakeActivities(red_runs=2)
        self.assertEqual((await self.run_workflow(fake)).outcome, "pr_opened")
        self.assertEqual([c.require_changes for c in fake.commits], [False, False, False])

    async def test_stops_when_issue_leaves_in_progress(self) -> None:
        fake = FakeActivities(red_runs=3, stop_after_checks=1)
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "stopped"))
        self.assertNotIn("open_pull_request", fake.calls)
        self.assertEqual(fake.calls.count("create_sandbox"), fake.calls.count("kill_sandbox"))
        self.assertEqual(fake.calls.count("implement_fix"), 1)

    async def test_stops_while_waiting_for_answer(self) -> None:
        fake = FakeActivities(
            red_runs=1, stop_after_checks=1, analyses=[FixAnalysis("causa", question="?")]
        )
        result = await self.run_workflow(fake)
        self.assertEqual(result, ImplementResult("ENG-12", "stopped"))
        self.assertEqual(len(fake.comments), 1)
        self.assertNotIn("implement_fix", fake.calls)

    async def test_fails_without_answer_in_seven_days(self) -> None:
        fake = FakeActivities(red_runs=1, analyses=[FixAnalysis("causa", question="?")])
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertNotIn("open_pull_request", fake.calls)
        self.assertNotIn("implement_fix", fake.calls)

    async def test_kills_sandbox_on_implement_failure(self) -> None:
        fake = FakeActivities(fail_implement=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls[-1], "kill_sandbox")
        self.assertNotIn("run_tests", fake.calls)
        self.assertNotIn("open_pull_request", fake.calls)

    async def test_kills_sandbox_on_commit_failure(self) -> None:
        fake = FakeActivities(fail_commit=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls[-1], "kill_sandbox")
        self.assertIn("commit_implementation", fake.calls)
        self.assertNotIn("open_pull_request", fake.calls)


if __name__ == "__main__":
    unittest.main()
