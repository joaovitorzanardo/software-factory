from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from temporalio.exceptions import ApplicationError

from software_factory.github.client import (
    API_VERSION,
    GITHUB_API_URL,
    REQUEST_TIMEOUT_S,
    GitHubClient,
)

TOKEN_REFRESH_MARGIN = timedelta(minutes=10)
INSTALLATION_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
JWT_BACKDATE_S = 60
JWT_LIFETIME_S = 540


def _utc_now() -> datetime:
    return datetime.now(UTC)


class GitHubApp:
    """GitHub App que emite tokens de instalação restritos a um repo (`owner/name`)."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.transport = transport
        self.clock = clock
        self._tokens: dict[str, tuple[str, datetime]] = {}

    def app_jwt(self) -> str:
        now = int(self.clock().timestamp())
        claims = {
            "iss": self.app_id,
            "iat": now - JWT_BACKDATE_S,
            "exp": now + JWT_LIFETIME_S,
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    async def installation_token(self, repo: str) -> str:
        key = repo.lower()
        cached = self._tokens.get(key)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN > self.clock():
            return cached[0]
        token, expires_at = await self._create_token(repo)
        self._tokens[key] = (token, expires_at)
        return token

    async def _create_token(self, repo: str) -> tuple[str, datetime]:
        name = repo.split("/", 1)[1]
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        # Erros de rede propagam: o Temporal retenta a activity.
        async with httpx.AsyncClient(
            base_url=GITHUB_API_URL, transport=self.transport, timeout=REQUEST_TIMEOUT_S
        ) as http:
            installation = await http.get(f"/repos/{repo}/installation", headers=headers)
            if installation.status_code == 404:
                raise ApplicationError(
                    f"GitHub App não está instalado em {repo}", non_retryable=True
                )
            installation_id = GitHubClient._check(installation).json()["id"]
            response = await http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers=headers,
                json={"repositories": [name], "permissions": INSTALLATION_PERMISSIONS},
            )
        body = GitHubClient._check(response).json()
        return body["token"], datetime.fromisoformat(body["expires_at"])
