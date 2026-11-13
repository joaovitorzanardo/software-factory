from typing import Any

import httpx
from temporalio.exceptions import ApplicationError

GITHUB_API_URL = "https://api.github.com"
REQUEST_TIMEOUT_S = 30
API_VERSION = "2022-11-28"


class GitHubClient:
    """Cliente REST mínimo do GitHub para um único repo (`owner/repo`)."""

    def __init__(
        self, token: str, repo: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.token = token
        self.repo = repo
        self.transport = transport

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            **kwargs.pop("headers", {}),
        }
        # Erros de rede propagam: o Temporal retenta a activity.
        async with httpx.AsyncClient(
            base_url=GITHUB_API_URL, transport=self.transport, timeout=REQUEST_TIMEOUT_S
        ) as http:
            return await http.request(method, path, headers=headers, **kwargs)

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        try:
            body = response.json()
            message = body.get("message") if isinstance(body, dict) else None
        except ValueError:
            message = None
        detail = f"HTTP {response.status_code}" + (f": {message}" if message else "")
        status = response.status_code
        rate_limited = status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        if status == 429 or status >= 500 or rate_limited:
            raise ApplicationError(f"GitHub indisponível: {detail}")
        # 401/403/404: token inválido ou sem acesso ao repo — retentar não resolve.
        raise ApplicationError(f"GitHub recusou a requisição: {detail}", non_retryable=True)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._check(await self._send(method, path, **kwargs))

    async def default_branch(self) -> str:
        response = await self._request("GET", f"/repos/{self.repo}")
        return response.json()["default_branch"]

    async def get_file(self, path: str, ref: str) -> str | None:
        """Conteúdo do arquivo em `ref`, ou None se a branch ou o arquivo não existem."""
        response = await self._send(
            "GET",
            f"/repos/{self.repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        return self._check(response).text

    async def find_open_pull(self, head: str) -> str | None:
        owner = self.repo.split("/")[0]
        response = await self._request(
            "GET",
            f"/repos/{self.repo}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        pulls = response.json()
        return pulls[0]["html_url"] if pulls else None

    async def create_pull(self, title: str, head: str, base: str, body: str) -> str:
        response = await self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return response.json()["html_url"]
