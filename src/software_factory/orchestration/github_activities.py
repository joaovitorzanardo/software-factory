from temporalio import activity

from software_factory.github.client import GitHubClient
from software_factory.orchestration.activities import plan_branch, plan_path
from software_factory.orchestration.models import PullRequestRequest


def pull_request_body(request: PullRequestRequest) -> str:
    return (
        f"Implementa {request.identifier} ({request.url or '-'}).\n\n"
        f"Plano: `{plan_path(request.identifier)}`\n\n"
        "🤖 Gerado pela software factory com [Claude Code](https://claude.com/claude-code)\n"
    )


class GitHubActivities:
    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @activity.defn
    async def plan_exists(self, identifier: str) -> bool:
        """True se o plano está commitado em factory/<ID>."""
        path = plan_path(identifier)
        # Valida o acesso ao repo antes: um 404 por token sem permissão não é "sem plano".
        await self.client.default_branch()
        content = await self.client.get_file(path, plan_branch(identifier))
        return bool(content and content.strip())

    @activity.defn
    async def open_pull_request(self, request: PullRequestRequest) -> str:
        """URL da PR factory/<ID> → branch default; reaproveita uma PR aberta (retry)."""
        head = plan_branch(request.identifier)
        existing = await self.client.find_open_pull(head)
        if existing:
            activity.logger.info("PR já aberta para %s: %s", head, existing)
            return existing
        url = await self.client.create_pull(
            title=f"{request.identifier}: {request.title}",
            head=head,
            base=await self.client.default_branch(),
            body=pull_request_body(request),
        )
        activity.logger.info("PR aberta: %s", url)
        return url
