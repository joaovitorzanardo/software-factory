import unittest
import uuid

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.models import (
    CloneRepoRequest,
    CommentRequest,
    CommitPlanRequest,
    MoveIssueRequest,
    PlanIssueInput,
    PlanRequest,
    PlanResult,
    RepoResolution,
    RepoTarget,
    UsageReportRequest,
)
from software_factory.orchestration.repo_resolution import repo_problem_comment
from software_factory.orchestration.workflow import PlanIssueWorkflow

ISSUE = PlanIssueInput(
    issue_id="issue-1",
    identifier="ENG-12",
    title="Adicionar login",
    description=None,
    url=None,
)
TARGET = RepoTarget("acme/app", "pytest")


class FakeActivities:
    def __init__(
        self,
        fail_plan: bool = False,
        fail_commit: bool = False,
        skip_planning_move: bool = False,
        fail_planning_move: bool = False,
        repo_problem: str = "",
    ) -> None:
        self.repo_problem = repo_problem
        self.fail_plan = fail_plan
        self.fail_commit = fail_commit
        self.skip_planning_move = skip_planning_move
        self.fail_planning_move = fail_planning_move
        self.calls: list[str] = []
        self.moves: list[MoveIssueRequest] = []
        self.clone_requests: list[CloneRepoRequest] = []
        self.commit_requests: list[CommitPlanRequest] = []
        self.comments: list[CommentRequest] = []

    @activity.defn(name="resolve_repo")
    async def resolve_repo(self, issue: PlanIssueInput) -> RepoResolution:
        self.calls.append("resolve_repo")
        if self.repo_problem:
            return RepoResolution(problem=self.repo_problem)
        return RepoResolution(target=TARGET)

    @activity.defn(name="post_comment")
    async def post_comment(self, request: CommentRequest) -> str:
        self.calls.append("post_comment")
        self.comments.append(request)
        return request.comment_id

    @activity.defn(name="move_issue")
    async def move_issue(self, request: MoveIssueRequest) -> bool:
        self.calls.append(f"move_issue:{request.to_state}")
        self.moves.append(request)
        if request.to_state == "In Planning":
            if self.fail_planning_move:
                raise ApplicationError("status não existe", non_retryable=True)
            if self.skip_planning_move:
                return False
        return True

    @activity.defn(name="create_sandbox")
    async def create_sandbox(self, issue_id: str) -> str:
        self.calls.append("create_sandbox")
        return "sbx-1"

    @activity.defn(name="clone_repo")
    async def clone_repo(self, request: CloneRepoRequest) -> None:
        self.calls.append("clone_repo")
        self.clone_requests.append(request)

    @activity.defn(name="generate_plan")
    async def generate_plan(self, request: PlanRequest) -> str:
        self.calls.append("generate_plan")
        if self.fail_plan:
            raise RuntimeError("claude falhou")
        return f"plano para {request.issue.identifier} em {request.sandbox_id}"

    @activity.defn(name="commit_plan")
    async def commit_plan(self, request: CommitPlanRequest) -> str:
        self.calls.append("commit_plan")
        self.commit_requests.append(request)
        if self.fail_commit:
            raise RuntimeError("push falhou")
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
        return request.comment_id


class TestPlanIssueWorkflow(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, fake: FakeActivities) -> PlanResult:
        task_queue = f"test-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[PlanIssueWorkflow],
                activities=[
                    fake.resolve_repo,
                    fake.post_comment,
                    fake.move_issue,
                    fake.create_sandbox,
                    fake.clone_repo,
                    fake.generate_plan,
                    fake.commit_plan,
                    fake.kill_sandbox,
                    fake.token_usage_report,
                    fake.upsert_comment,
                ],
            ):
                return await env.client.execute_workflow(
                    PlanIssueWorkflow.run,
                    ISSUE,
                    id=f"plan-issue-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    async def test_happy_path(self) -> None:
        fake = FakeActivities()
        result = await self.run_workflow(fake)
        self.assertEqual(
            result,
            PlanResult(
                identifier="ENG-12",
                plan="plano para ENG-12 em sbx-1",
                sandbox_id="sbx-1",
                branch="factory/ENG-12",
            ),
        )
        self.assertEqual(
            fake.calls,
            [
                "resolve_repo",
                "move_issue:In Planning",
                "create_sandbox",
                "clone_repo",
                "generate_plan",
                "commit_plan",
                "kill_sandbox",
                "token_usage_report:final=False",
                "upsert_comment",
                "move_issue:In Progress",
            ],
        )
        self.assertEqual(
            fake.moves,
            [
                MoveIssueRequest(
                    "issue-1",
                    to_state="In Planning",
                    from_state="To Do",
                    require_states=("In Progress",),
                ),
                MoveIssueRequest("issue-1", to_state="In Progress"),
            ],
        )

    async def test_publishes_token_usage_before_in_progress(self) -> None:
        fake = FakeActivities()
        await self.run_workflow(fake)
        self.assertEqual(
            fake.calls[-5:],
            [
                "commit_plan",
                "kill_sandbox",
                "token_usage_report:final=False",
                "upsert_comment",
                "move_issue:In Progress",
            ],
        )

    async def test_plans_even_when_planning_move_is_skipped(self) -> None:
        fake = FakeActivities(skip_planning_move=True)
        await self.run_workflow(fake)
        self.assertIn("commit_plan", fake.calls)
        self.assertEqual(fake.calls[-1], "move_issue:In Progress")

    async def test_planning_move_failure_creates_no_sandbox(self) -> None:
        fake = FakeActivities(fail_planning_move=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls, ["resolve_repo", "move_issue:In Planning"])

    async def test_kills_sandbox_on_failure(self) -> None:
        fake = FakeActivities(fail_plan=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls[-1], "kill_sandbox")
        self.assertIn("generate_plan", fake.calls)
        self.assertNotIn("move_issue:In Progress", fake.calls)

    async def test_kills_sandbox_on_commit_failure(self) -> None:
        fake = FakeActivities(fail_commit=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls[-1], "kill_sandbox")
        self.assertIn("commit_plan", fake.calls)
        self.assertNotIn("move_issue:In Progress", fake.calls)

    async def test_happy_path_passes_target(self) -> None:
        fake = FakeActivities()
        await self.run_workflow(fake)
        self.assertEqual(fake.calls[0], "resolve_repo")
        self.assertEqual(fake.clone_requests, [CloneRepoRequest("sbx-1", TARGET)])
        self.assertEqual(fake.commit_requests[0].target, TARGET)

    async def test_repo_problem_comments_and_fails(self) -> None:
        fake = FakeActivities(repo_problem="o projeto App não tem link para um repo do GitHub")
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        # Nem move_issue, nem sandbox.
        self.assertEqual(fake.calls, ["resolve_repo", "post_comment"])
        self.assertEqual(fake.comments[0].issue_id, "issue-1")
        self.assertIn("o projeto App não tem link", fake.comments[0].body)


class TestRepoProblemComment(unittest.TestCase):
    def test_names_problem_and_how_to_fix(self) -> None:
        body = repo_problem_comment("o projeto App não tem link")
        self.assertTrue(body.startswith("**software-factory não sabe em qual repo trabalhar.**"))
        self.assertIn("\n\no projeto App não tem link\n\n", body)
        self.assertIn("`.factory.toml` na branch default do repo", body)
        self.assertIn("dispare a etapa de novo", body)


if __name__ == "__main__":
    unittest.main()
