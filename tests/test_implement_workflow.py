import unittest
import uuid

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from software_factory.orchestration.implement_workflow import ImplementIssueWorkflow
from software_factory.orchestration.models import (
    CloneBranchRequest,
    CommitImplementationRequest,
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
        tests_red: bool = False,
        fail_implement: bool = False,
        fail_commit: bool = False,
    ) -> None:
        self.not_in_progress = not_in_progress
        self.no_plan = no_plan
        self.tests_red = tests_red
        self.fail_implement = fail_implement
        self.fail_commit = fail_commit
        self.calls: list[str] = []
        self.moves: list[MoveIssueRequest] = []
        self.commits: list[CommitImplementationRequest] = []
        self.pulls: list[PullRequestRequest] = []

    @activity.defn(name="move_issue")
    async def move_issue(self, request: MoveIssueRequest) -> bool:
        self.calls.append(f"move_issue:{request.to_state}")
        self.moves.append(request)
        return not (self.not_in_progress and request.from_state == "In Progress")

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
        return "abc123"

    @activity.defn(name="implement_plan")
    async def implement_plan(self, request: ImplementRequest) -> str:
        self.calls.append("implement_plan")
        if self.fail_implement:
            raise ApplicationError("claude falhou", non_retryable=True)
        return "Implementado."

    @activity.defn(name="run_tests")
    async def run_tests(self, sandbox_id: str) -> TestRunResult:
        self.calls.append("run_tests")
        return TestRunResult(passed=not self.tests_red, output="FAILED test_a")

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
        self.assertEqual(fake.commits[0].base_sha, "abc123")
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

    async def test_red_tests_open_no_pr(self) -> None:
        fake = FakeActivities(tests_red=True)
        with self.assertRaises(WorkflowFailureError):
            await self.run_workflow(fake)
        self.assertEqual(fake.calls[-1], "kill_sandbox")
        self.assertIn("run_tests", fake.calls)
        for call in ("commit_implementation", "open_pull_request", "move_issue:In Review"):
            self.assertNotIn(call, fake.calls)

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
