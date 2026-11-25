from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.github.client import GitHubClients
from software_factory.linear.client import LinearClient
from software_factory.orchestration.models import PlanIssueInput, RepoResolution, RepoTarget
from software_factory.orchestration.repos import (
    FACTORY_CONFIG_PATH,
    FactoryConfigError,
    linked_repos,
    parse_factory_config,
    pinned_repos,
)

# Links do projeto: `externalLinks` (Resources do projeto no Linear).
ISSUE_REPO_QUERY = """
query($id: String!) {
  issue(id: $id) {
    project { name externalLinks { nodes { url } } }
    attachments(first: 100) { nodes { url } }
  }
}
"""


class RepoActivities:
    def __init__(self, linear: LinearClient, clients: GitHubClients) -> None:
        self.linear = linear
        self.clients = clients

    @activity.defn
    async def resolve_repo(self, issue: PlanIssueInput) -> RepoResolution:
        """Resolve o repo alvo (pin da issue > link do projeto) e lê o `.factory.toml` da branch default."""
        data = await self.linear.execute(ISSUE_REPO_QUERY, {"id": issue.issue_id})
        found = data.get("issue")
        if found is None:
            raise ApplicationError(
                f"issue {issue.issue_id} não encontrada no Linear", non_retryable=True
            )
        repo_or_problem = self._pick_repo(found)
        if isinstance(repo_or_problem, RepoResolution):
            return repo_or_problem
        repo = repo_or_problem
        try:
            client = await self.clients(repo)
            default = await client.default_branch()
            text = await client.get_file(FACTORY_CONFIG_PATH, default)
        except ApplicationError as error:
            if error.non_retryable:
                return RepoResolution(
                    problem=f"sem acesso ao repo {repo} pelo GitHub: {error.message}"
                )
            raise
        if text is None:
            return RepoResolution(
                problem=f"o repo {repo} não tem {FACTORY_CONFIG_PATH} na branch default ({default})"
            )
        try:
            test_command = parse_factory_config(text)
        except FactoryConfigError as error:
            return RepoResolution(problem=str(error))
        return RepoResolution(target=RepoTarget(repo=repo, test_command=test_command))

    @staticmethod
    def _pick_repo(found: dict) -> "str | RepoResolution":
        pins = pinned_repos(n["url"] for n in found["attachments"]["nodes"])
        if len(pins) > 1:
            return RepoResolution(
                problem=(
                    f"a issue tem anexos de branch factory/ em mais de um repo "
                    f"({', '.join(pins)}); remova os que não valem"
                )
            )
        if pins:
            return pins[0]
        project = found.get("project")
        if project is None:
            return RepoResolution(
                problem=(
                    "a issue não está em nenhum projeto do Linear; coloque-a num projeto "
                    "com o link do repo nos Resources"
                )
            )
        name = project["name"]
        repos = linked_repos(n["url"] for n in project["externalLinks"]["nodes"])
        if not repos:
            return RepoResolution(
                problem=(
                    f"o projeto {name} não tem link para um repo do GitHub nos Resources "
                    "(https://github.com/<owner>/<repo>)"
                )
            )
        if len(repos) > 1:
            return RepoResolution(
                problem=(
                    f"o projeto {name} tem links para mais de um repo do GitHub "
                    f"({', '.join(repos)}); deixe só um"
                )
            )
        return repos[0]
