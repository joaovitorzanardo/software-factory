import unittest
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.orchestration.github_activities import GitHubActivities
from software_factory.orchestration.models import (
    MergeRequest,
    MergeResult,
    PullInfo,
    PullRequestRequest,
)

PLAN = "docs/superpowers/plans/ENG-12.md"
LINEAR_URL = "https://linear.app/acme/issue/ENG-12"


class FakeGitHubClient:
    def __init__(
        self,
        file: str | None = "# Plano",
        open_pull: dict[str, Any] | None = None,
        branch_error: Exception | None = None,
        pull: dict[str, Any] | None = None,
        merge_result: tuple[bool, str] = (True, ""),
    ) -> None:
        self.pull = pull or {}
        self.pull_after_merge_attempt: dict[str, Any] | None = None
        self.merge_result = merge_result
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

    async def find_open_pull(self, head: str) -> dict[str, Any] | None:
        self.calls.append(("find_open_pull", head))
        return self.open_pull

    async def get_pull(self, number: int) -> dict[str, Any]:
        self.calls.append(("get_pull", number))
        if self.pull_after_merge_attempt and "merge_pull" in self.names():
            return self.pull_after_merge_attempt
        return self.pull

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def merge_base(self, base: str, head: str) -> str:
        self.calls.append(("merge_base", (base, head)))
        return "base999"

    async def merge_pull(self, number: int, sha: str, title: str) -> tuple[bool, str]:
        self.calls.append(("merge_pull", {"number": number, "sha": sha, "title": title}))
        return self.merge_result

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
        client = FakeGitHubClient(open_pull={"html_url": "https://github.com/acme/app/pull/7"})
        url = await self.env.run(
            GitHubActivities(client).open_pull_request,
            PullRequestRequest("ENG-12", "Adicionar login", None),
        )
        self.assertEqual(url, "https://github.com/acme/app/pull/7")
        self.assertNotIn("create_pull", self.names(client))
        self.assertEqual(client.calls[0], ("find_open_pull", "factory/ENG-12"))

    async def test_pull_request_info(self) -> None:
        pull = {
            "number": 7,
            "html_url": "https://github.com/acme/app/pull/7",
            "head": {"sha": "head123"},
            "base": {"ref": "main"},
        }
        client = FakeGitHubClient(open_pull=pull)
        info = await self.env.run(GitHubActivities(client).pull_request_info, "ENG-12")
        self.assertEqual(
            info, PullInfo(7, "https://github.com/acme/app/pull/7", "head123", "main", "base999")
        )
        self.assertEqual(client.calls[0], ("find_open_pull", "factory/ENG-12"))
        self.assertIn(("merge_base", ("main", "head123")), client.calls)

    async def test_pull_request_info_none_without_open_pull(self) -> None:
        client = FakeGitHubClient()
        self.assertIsNone(await self.env.run(GitHubActivities(client).pull_request_info, "ENG-12"))
        self.assertNotIn("merge_base", self.names(client))

    def merge_pull_data(self, **overrides: Any) -> dict[str, Any]:
        pull = {
            "number": 7,
            "state": "open",
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {"sha": "head123"},
        }
        return {**pull, **overrides}

    def merge_request(self) -> MergeRequest:
        return MergeRequest("ENG-12", "Adicionar login", 7, "head123")

    async def merge(self, client: FakeGitHubClient) -> MergeResult:
        return await self.env.run(GitHubActivities(client).merge_pull_request, self.merge_request())

    async def test_merge_already_merged_is_noop(self) -> None:
        client = FakeGitHubClient(pull=self.merge_pull_data(merged=True, state="closed"))
        self.assertEqual(await self.merge(client), MergeResult(True))
        self.assertNotIn("merge_pull", self.names(client))

    async def test_merge_closed_without_merge(self) -> None:
        client = FakeGitHubClient(pull=self.merge_pull_data(state="closed"))
        self.assertEqual(await self.merge(client), MergeResult(False, reason="PR fechada sem merge"))
        self.assertNotIn("merge_pull", self.names(client))

    async def test_merge_head_changed(self) -> None:
        client = FakeGitHubClient(pull=self.merge_pull_data(head={"sha": "other"}))
        self.assertEqual(await self.merge(client), MergeResult(False, head_changed=True))
        self.assertNotIn("merge_pull", self.names(client))

    async def test_merge_mergeable_unknown_is_retryable(self) -> None:
        client = FakeGitHubClient(pull=self.merge_pull_data(mergeable=None))
        with self.assertRaises(ApplicationError) as ctx:
            await self.merge(client)
        self.assertFalse(ctx.exception.non_retryable)
        self.assertNotIn("merge_pull", self.names(client))

    async def test_merge_conflict_returns_reason(self) -> None:
        client = FakeGitHubClient(
            pull=self.merge_pull_data(mergeable=False, mergeable_state="dirty")
        )
        self.assertEqual(
            await self.merge(client), MergeResult(False, reason="não mergeável (dirty)")
        )
        self.assertNotIn("merge_pull", self.names(client))

    async def test_merge_squash_with_reviewed_sha(self) -> None:
        client = FakeGitHubClient(pull=self.merge_pull_data())
        self.assertEqual(await self.merge(client), MergeResult(True))
        self.assertEqual(
            dict(client.calls)["merge_pull"],
            {"number": 7, "sha": "head123", "title": "ENG-12: Adicionar login (#7)"},
        )

    async def test_merge_rejected_by_github_is_not_head_changed(self) -> None:
        client = FakeGitHubClient(
            pull=self.merge_pull_data(), merge_result=(False, "Pull Request is not mergeable")
        )
        result = await self.merge(client)
        self.assertEqual(result, MergeResult(False, reason="Pull Request is not mergeable"))

    async def test_merge_head_moved_during_merge_is_head_changed(self) -> None:
        client = FakeGitHubClient(
            pull=self.merge_pull_data(),
            merge_result=(False, "Head branch was modified. Review and try the merge again."),
        )
        client.pull_after_merge_attempt = self.merge_pull_data(head={"sha": "newer"})
        self.assertEqual(await self.merge(client), MergeResult(False, head_changed=True))


if __name__ == "__main__":
    unittest.main()
