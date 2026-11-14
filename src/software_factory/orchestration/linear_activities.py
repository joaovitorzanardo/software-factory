from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.linear.client import LinearClient
from software_factory.orchestration.models import CommentRequest, MoveIssueRequest

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

    @activity.defn
    async def post_comment(self, request: CommentRequest) -> str:
        """Id do comentário; idempotente: se `comment_id` já existe, não cria de novo."""
        data = await self.client.execute(
            COMMENT_EXISTS_QUERY, {"issueId": request.issue_id, "commentId": request.comment_id}
        )
        issue = data.get("issue")
        if issue is None:
            raise ApplicationError(
                f"issue {request.issue_id} não encontrada no Linear", non_retryable=True
            )
        # Retry depois de um comentário já criado: devolve o id sem mutation.
        if issue["comments"]["nodes"]:
            return request.comment_id

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
