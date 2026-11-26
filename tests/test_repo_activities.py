import unittest
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.orchestration.models import PlanIssueInput, RepoResolution, RepoTarget
from software_factory.orchestration.repo_activities import RepoActivities

ISSUE = PlanIssueInput(
    issue_id="issue-1", identifier="ENG-12", title="T", description=None, url=None
)


def project(*urls: str, name: str = "App") -> dict[str, Any]:
    return {"name": name, "externalLinks": {"nodes": [{"url": u} for u in urls]}}


class FakeLinear:
    def __init__(self, issue: dict[str, Any] | None) -> None:
        self.issue = issue

    async def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"issue": self.issue}


class FakeClient:
    def __init__(self, config: str | None, error: ApplicationError | None = None) -> None:
        self.config = config
        self.error = error
        self.files: list[tuple[str, str]] = []

    async def default_branch(self) -> str:
        if self.error:
            raise self.error
        return "main"

    async def get_file(self, path: str, ref: str) -> str | None:
        self.files.append((path, ref))
        return self.config


class TestResolveRepo(unittest.IsolatedAsyncioTestCase):
    async def resolve(
        self,
        proj: dict[str, Any] | None,
        pins: list[str] | None = None,
        config: str | None = 'test_command = "pytest"',
        client_error: ApplicationError | None = None,
        clients_error: ApplicationError | None = None,
    ) -> RepoResolution:
        self.client = FakeClient(config, client_error)
        self.requested: list[str] = []

        async def clients(repo: str) -> FakeClient:
            self.requested.append(repo)
            if clients_error:
                raise clients_error
            return self.client

        issue = {
            "project": proj,
            "attachments": {"nodes": [{"url": u} for u in (pins or [])]},
        }
        activities = RepoActivities(FakeLinear(issue), clients)  # type: ignore[arg-type]
        return await ActivityEnvironment().run(activities.resolve_repo, ISSUE)

    async def test_resolves_from_project_link(self):
        result = await self.resolve(project("https://github.com/acme/app"))
        self.assertEqual(result, RepoResolution(target=RepoTarget("acme/app", "pytest")))
        self.assertEqual(self.requested, ["acme/app"])

    async def test_reads_config_from_default_branch(self):
        await self.resolve(project("https://github.com/acme/app"))
        self.assertEqual(self.client.files, [(".factory.toml", "main")])

    async def test_pin_wins_over_project(self):
        result = await self.resolve(
            project("https://github.com/acme/app"),
            pins=["https://github.com/acme/old/tree/factory/ENG-12"],
        )
        self.assertEqual(result.target.repo, "acme/old")

    async def test_two_pins_is_problem(self):
        result = await self.resolve(
            project("https://github.com/acme/app"),
            pins=[
                "https://github.com/acme/a/tree/factory/ENG-12",
                "https://github.com/acme/b/tree/factory/ENG-12",
            ],
        )
        self.assertIsNone(result.target)
        self.assertIn("acme/a", result.problem)
        self.assertIn("acme/b", result.problem)

    async def test_no_project_is_problem(self):
        result = await self.resolve(None)
        self.assertIsNone(result.target)
        self.assertIn("não está em nenhum projeto", result.problem)

    async def test_no_github_link_is_problem(self):
        result = await self.resolve(project("https://docs.google.com/document/d/1"))
        self.assertIsNone(result.target)
        self.assertIn("App", result.problem)

    async def test_two_repos_is_problem(self):
        result = await self.resolve(
            project("https://github.com/acme/app", "https://github.com/acme/api")
        )
        self.assertIsNone(result.target)
        self.assertIn("acme/app", result.problem)
        self.assertIn("acme/api", result.problem)

    async def test_same_repo_twice_resolves(self):
        result = await self.resolve(
            project("https://github.com/acme/app", "https://github.com/Acme/App/issues")
        )
        self.assertEqual(result.target.repo, "acme/app")

    async def test_missing_config_is_problem(self):
        result = await self.resolve(project("https://github.com/acme/app"), config=None)
        self.assertIsNone(result.target)
        self.assertIn(".factory.toml", result.problem)
        self.assertIn("main", result.problem)

    async def test_invalid_config_is_problem(self):
        result = await self.resolve(
            project("https://github.com/acme/app"), config="test_command = 3"
        )
        self.assertIsNone(result.target)
        self.assertIn(".factory.toml", result.problem)

    async def test_no_access_is_problem(self):
        result = await self.resolve(
            project("https://github.com/acme/app"),
            clients_error=ApplicationError(
                "GitHub App não está instalado em acme/app", non_retryable=True
            ),
        )
        self.assertIn("acme/app", result.problem)
        self.assertIsNone(result.target)

    async def test_retryable_github_error_propagates(self):
        with self.assertRaises(ApplicationError):
            await self.resolve(
                project("https://github.com/acme/app"),
                client_error=ApplicationError("GitHub indisponível"),
            )

    async def test_issue_not_found_is_non_retryable(self):
        activities = RepoActivities(FakeLinear(None), None)  # type: ignore[arg-type]
        with self.assertRaises(ApplicationError) as ctx:
            await ActivityEnvironment().run(activities.resolve_repo, ISSUE)
        self.assertTrue(ctx.exception.non_retryable)
