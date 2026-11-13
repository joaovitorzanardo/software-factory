import unittest
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.orchestration.github_activities import GitHubActivities
from software_factory.orchestration.models import PullRequestRequest

PLAN = "docs/superpowers/plans/ENG-12.md"
LINEAR_URL = "https://linear.app/acme/issue/ENG-12"


class FakeGitHubClient:
    def __init__(
        self,
        file: str | None = "# Plano",
        open_pull: str | None = None,
        branch_error: Exception | None = None,
    ) -> None:
        self.file = file
        self.open_pull = open_pull
        self.branch_error = branch_error
        self.calls: list[tuple[str, Any]] = []

    async def default_branch(self) -> str:
        self.calls.append(("default_branch", None))
        if self.branch_error:
            raise self.branch_error
        return "main"

    async def get_file(self, path: str, ref: str) -> str | None:
        self.calls.append(("get_file", (path, ref)))
        return self.file

    async def find_open_pull(self, head: str) -> str | None:
        self.calls.append(("find_open_pull", head))
        return self.open_pull

    async def create_pull(self, title: str, head: str, base: str, body: str) -> str:
        self.calls.append(("create_pull", {"title": title, "head": head, "base": base, "body": body}))
        return "https://github.com/acme/app/pull/8"


class TestGitHubActivities(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = ActivityEnvironment()

    def names(self, client: FakeGitHubClient) -> list[str]:
        return [name for name, _ in client.calls]

    async def test_plan_exists(self) -> None:
        client = FakeGitHubClient()
        self.assertTrue(await self.env.run(GitHubActivities(client).plan_exists, "ENG-12"))
        self.assertEqual(client.calls[0], ("default_branch", None))
        self.assertEqual(client.calls[1], ("get_file", (PLAN, "factory/ENG-12")))

    async def test_plan_missing(self) -> None:
        for content in (None, "  \n"):
            with self.subTest(content=content):
                client = FakeGitHubClient(file=content)
                self.assertFalse(await self.env.run(GitHubActivities(client).plan_exists, "ENG-12"))

    async def test_plan_exists_propagates_repo_access_error(self) -> None:
        error = ApplicationError("GitHub recusou a requisição: HTTP 404", non_retryable=True)
        client = FakeGitHubClient(branch_error=error)
        with self.assertRaises(ApplicationError) as ctx:
            await self.env.run(GitHubActivities(client).plan_exists, "ENG-12")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertNotIn("get_file", self.names(client))

    async def test_plan_exists_rejects_bad_identifier(self) -> None:
        client = FakeGitHubClient()
        with self.assertRaises(ApplicationError) as ctx:
            await self.env.run(GitHubActivities(client).plan_exists, "ENG-12; rm")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertEqual(client.calls, [])

    async def test_open_pull_request_creates(self) -> None:
        client = FakeGitHubClient()
        url = await self.env.run(
            GitHubActivities(client).open_pull_request,
            PullRequestRequest("ENG-12", "Adicionar login", LINEAR_URL),
        )
        self.assertEqual(url, "https://github.com/acme/app/pull/8")
        create = dict(client.calls)["create_pull"]
        self.assertEqual(create["title"], "ENG-12: Adicionar login")
        self.assertEqual(create["head"], "factory/ENG-12")
        self.assertEqual(create["base"], "main")
        self.assertIn(LINEAR_URL, create["body"])
        self.assertIn(PLAN, create["body"])

    async def test_open_pull_request_reuses_existing(self) -> None:
        client = FakeGitHubClient(open_pull="https://github.com/acme/app/pull/7")
        url = await self.env.run(
            GitHubActivities(client).open_pull_request,
            PullRequestRequest("ENG-12", "Adicionar login", None),
        )
        self.assertEqual(url, "https://github.com/acme/app/pull/7")
        self.assertNotIn("create_pull", self.names(client))
        self.assertEqual(client.calls[0], ("find_open_pull", "factory/ENG-12"))


if __name__ == "__main__":
    unittest.main()
