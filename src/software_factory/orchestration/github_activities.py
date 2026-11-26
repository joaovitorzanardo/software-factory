from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.github.client import GitHubClients
from software_factory.orchestration.activities import plan_branch, plan_path
from software_factory.orchestration.models import (
    BranchRequest,
    MergeRequest,
    MergeResult,
    PullInfo,
    PullRequestRequest,
)


def pull_request_body(request: PullRequestRequest) -> str:
    return (
        f"Implementa {request.identifier} ({request.url or '-'}).\n\n"
        f"Plano: `{plan_path(request.identifier)}`\n\n"
        "🤖 Gerado pela software factory com [Claude Code](https://claude.com/claude-code)\n"
    )


class GitHubActivities:
    """Um `GitHubClient` por chamada, para o repo do `target` da requisição."""

    def __init__(self, clients: GitHubClients) -> None:
        self.clients = clients

    @activity.defn
    async def plan_exists(self, request: BranchRequest) -> bool:
        """True se o plano está commitado em factory/<ID>."""
        path = plan_path(request.identifier)
        client = await self.clients(request.target.repo)
        # Valida o acesso ao repo antes: um 404 por token sem permissão não é "sem plano".
        await client.default_branch()
        content = await client.get_file(path, plan_branch(request.identifier))
        return bool(content and content.strip())

    @activity.defn
    async def open_pull_request(self, request: PullRequestRequest) -> str:
        """URL da PR factory/<ID> → branch default; reaproveita uma PR aberta (retry)."""
        head = plan_branch(request.identifier)
        client = await self.clients(request.target.repo)
        existing = await client.find_open_pull(head)
        if existing:
            url = existing["html_url"]
            activity.logger.info("PR já aberta para %s: %s", head, url)
            return url
        url = await client.create_pull(
            title=f"{request.identifier}: {request.title}",
            head=head,
            base=await client.default_branch(),
            body=pull_request_body(request),
        )
        activity.logger.info("PR aberta: %s", url)
        return url

    @activity.defn
    async def pull_request_info(self, request: BranchRequest) -> PullInfo | None:
        """PR aberta de factory/<ID> com o merge-base contra a base, ou None."""
        client = await self.clients(request.target.repo)
        pull = await client.find_open_pull(plan_branch(request.identifier))
        if pull is None:
            return None
        head_sha = pull["head"]["sha"]
        base_ref = pull["base"]["ref"]
        return PullInfo(
            number=pull["number"],
            url=pull["html_url"],
            head_sha=head_sha,
            base_ref=base_ref,
            base_sha=await client.merge_base(base_ref, head_sha),
        )

    @activity.defn
    async def merge_pull_request(self, request: MergeRequest) -> MergeResult:
        """Squash merge da PR no sha revisado; idempotente (PR já mergeada → sucesso)."""
        client = await self.clients(request.target.repo)
        pull = await client.get_pull(request.number)
        if pull.get("merged"):
            return MergeResult(True)
        if pull.get("state") == "closed":
            return MergeResult(False, reason="PR fechada sem merge")
        if pull["head"]["sha"] != request.head_sha:
            return MergeResult(False, head_changed=True)
        mergeable = pull.get("mergeable")
        if mergeable is None:
            # O GitHub ainda calcula a mergeabilidade: o retry resolve.
            raise ApplicationError("mergeable ainda não calculado pelo GitHub")
        if mergeable is False:
            return MergeResult(False, reason=f"não mergeável ({pull.get('mergeable_state')})")
        title = f"{request.identifier}: {request.title} (#{request.number})"
        merged, message = await client.merge_pull(request.number, request.head_sha, title)
        if merged:
            return MergeResult(True)
        # 409 = head mudou entre o get_pull e o merge; 405 = recusa por outro motivo.
        current = await client.get_pull(request.number)
        if current["head"]["sha"] != request.head_sha:
            return MergeResult(False, head_changed=True)
        return MergeResult(False, reason=message)
