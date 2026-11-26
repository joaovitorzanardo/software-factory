from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.linear.client import LinearClient
from software_factory.orchestration.models import (
    AttachLinkRequest,
    CommentRequest,
    CreateIssueRequest,
    FollowUpIssue,
    MoveIssueRequest,
)

ISSUE_STATES_QUERY = """
query($id: String!) {
  issue(id: $id) { state { id name } team { states { nodes { id name } } } }
}
"""

UPDATE_STATE_MUTATION = """
mutation($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) { success }
}
"""

COMMENT_EXISTS_QUERY = """
query($issueId: String!, $commentId: ID!) {
  issue(id: $issueId) { comments(filter: {id: {eq: $commentId}}) { nodes { id } } }
}
"""

COMMENT_CREATE_MUTATION = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}
"""

COMMENT_UPDATE_MUTATION = """
mutation($id: String!, $body: String!) { commentUpdate(id: $id, input: {body: $body}) { success } }
"""

ISSUE_EXISTS_QUERY = """
query($id: ID!) {
  issues(filter: {id: {eq: $id}}) { nodes { identifier url } }
}
"""

ISSUE_TEAM_QUERY = """
query($originId: String!) {
  issue(id: $originId) { team { id } }
}
"""

ISSUE_CREATE_MUTATION = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { identifier url } }
}
"""

ATTACHMENTS_QUERY = """
query($id: String!) {
  issue(id: $id) { attachments(first: 100) { nodes { url } } }
}
"""

ATTACHMENT_CREATE_MUTATION = """
mutation($input: AttachmentCreateInput!) {
  attachmentCreate(input: $input) { success }
}
"""


class LinearActivities:
    def __init__(self, client: LinearClient) -> None:
        self.client = client

    @activity.defn
    async def move_issue(self, request: MoveIssueRequest) -> bool:
        """True se a issue terminou em `to_state`; False se a guarda `from_state` pulou."""
        data = await self.client.execute(ISSUE_STATES_QUERY, {"id": request.issue_id})
        issue = data.get("issue")
        if issue is None:
            raise ApplicationError(
                f"issue {request.issue_id} não encontrada no Linear", non_retryable=True
            )
        current = issue["state"]["name"]
        states = issue["team"]["states"]["nodes"]
        names = {s["name"].lower() for s in states}
        missing = [
            name for name in (request.to_state, *request.require_states) if name.lower() not in names
        ]
        if missing:
            available = ", ".join(s["name"] for s in states)
            raise ApplicationError(
                f"status {', '.join(map(repr, missing))} não existe no time da issue"
                f" (há: {available})",
                non_retryable=True,
            )
        # Já no destino (inclusive retry após uma mutation aplicada): nada a fazer.
        if current.lower() == request.to_state.lower():
            return True
        if request.from_state and current.lower() != request.from_state.lower():
            activity.logger.info(
                "issue %s está em %r, não em %r; não move para %r",
                request.issue_id,
                current,
                request.from_state,
                request.to_state,
            )
            return False

        target = next(s for s in states if s["name"].lower() == request.to_state.lower())
        result = await self.client.execute(
            UPDATE_STATE_MUTATION, {"id": request.issue_id, "stateId": target["id"]}
        )
        if not result["issueUpdate"]["success"]:
            raise ApplicationError(f"Linear não atualizou o status da issue {request.issue_id}")
        activity.logger.info("issue %s: %r → %r", request.issue_id, current, target["name"])
        return True

    async def _comment_exists(self, issue_id: str, comment_id: str) -> bool:
        data = await self.client.execute(
            COMMENT_EXISTS_QUERY, {"issueId": issue_id, "commentId": comment_id}
        )
        issue = data.get("issue")
        if issue is None:
            raise ApplicationError(f"issue {issue_id} não encontrada no Linear", non_retryable=True)
        return bool(issue["comments"]["nodes"])

    async def _create_comment(self, request: CommentRequest) -> str:
        result = await self.client.execute(
            COMMENT_CREATE_MUTATION,
            {
                "input": {
                    "id": request.comment_id,
                    "issueId": request.issue_id,
                    "body": request.body,
                }
            },
        )
        created = result["commentCreate"]
        if not created["success"]:
            raise ApplicationError(f"Linear não criou o comentário na issue {request.issue_id}")
        activity.logger.info(
            "comentário %s criado na issue %s", request.comment_id, request.issue_id
        )
        return created["comment"]["id"]

    @activity.defn
    async def post_comment(self, request: CommentRequest) -> str:
        """Id do comentário; idempotente: se `comment_id` já existe, não cria de novo."""
        # Retry depois de um comentário já criado: devolve o id sem mutation.
        if await self._comment_exists(request.issue_id, request.comment_id):
            return request.comment_id

        return await self._create_comment(request)

    @activity.defn
    async def upsert_comment(self, request: CommentRequest) -> str:
        """Id do comentário; cria se `comment_id` não existe, senão troca o corpo."""
        if not await self._comment_exists(request.issue_id, request.comment_id):
            return await self._create_comment(request)

        result = await self.client.execute(
            COMMENT_UPDATE_MUTATION, {"id": request.comment_id, "body": request.body}
        )
        if not result["commentUpdate"]["success"]:
            raise ApplicationError(
                f"Linear não atualizou o comentário {request.comment_id} da issue {request.issue_id}"
            )
        activity.logger.info(
            "comentário %s atualizado na issue %s", request.comment_id, request.issue_id
        )
        return request.comment_id

    @activity.defn
    async def create_issue(self, request: CreateIssueRequest) -> FollowUpIssue:
        """Follow-up no time da issue de origem; idempotente pelo id gerado pelo cliente.

        Criada sem labels: as de gatilho (Feature/Bug) fariam a factory iniciar nela sozinha.
        """
        existing = await self.client.execute(ISSUE_EXISTS_QUERY, {"id": request.new_issue_id})
        nodes = existing["issues"]["nodes"]
        # Retry depois de uma issue já criada: devolve a existente sem mutation.
        if nodes:
            return FollowUpIssue(identifier=nodes[0]["identifier"], url=nodes[0]["url"])

        data = await self.client.execute(ISSUE_TEAM_QUERY, {"originId": request.origin_issue_id})
        origin = data.get("issue")
        if origin is None:
            raise ApplicationError(
                f"issue {request.origin_issue_id} não encontrada no Linear", non_retryable=True
            )

        result = await self.client.execute(
            ISSUE_CREATE_MUTATION,
            {
                "input": {
                    "id": request.new_issue_id,
                    "teamId": origin["team"]["id"],
                    "title": request.title,
                    "description": request.description,
                }
            },
        )
        created = result["issueCreate"]
        if not created["success"]:
            raise ApplicationError(
                f"Linear não criou o follow-up da issue {request.origin_issue_id}"
            )
        issue = created["issue"]
        activity.logger.info(
            "follow-up %s criado a partir da issue %s", issue["identifier"], request.origin_issue_id
        )
        return FollowUpIssue(identifier=issue["identifier"], url=issue["url"])

    @activity.defn
    async def attach_link(self, request: AttachLinkRequest) -> None:
        """Anexa o link à issue; idempotente pela URL (já anexada: sem mutation)."""
        data = await self.client.execute(ATTACHMENTS_QUERY, {"id": request.issue_id})
        issue = data.get("issue")
        if issue is None:
            raise ApplicationError(
                f"issue {request.issue_id} não encontrada no Linear", non_retryable=True
            )
        if any(node["url"] == request.url for node in issue["attachments"]["nodes"]):
            return

        result = await self.client.execute(
            ATTACHMENT_CREATE_MUTATION,
            {
                "input": {
                    "issueId": request.issue_id,
                    "url": request.url,
                    "title": request.title,
                    "subtitle": request.subtitle,
                }
            },
        )
        if not result["attachmentCreate"]["success"]:
            raise ApplicationError(f"Linear não anexou o link na issue {request.issue_id}")
        activity.logger.info("link %s anexado à issue %s", request.url, request.issue_id)
