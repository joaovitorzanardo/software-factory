import json
import unittest
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from temporalio.exceptions import ApplicationError

from software_factory.github.app import INSTALLATION_PERMISSIONS, GitHubApp

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class TestGitHubApp(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(65537, 2048)
        cls.private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        cls.public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def make_app(
        self,
        token_status: int = 201,
        installation_status: int = 200,
    ) -> tuple[GitHubApp, Clock, list[httpx.Request]]:
        requests: list[httpx.Request] = []
        clock = Clock()

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                if installation_status != 200:
                    return httpx.Response(installation_status, json={"message": "Not Found"})
                return httpx.Response(200, json={"id": 9})
            if token_status != 201:
                return httpx.Response(token_status, json={"message": "Bad Gateway"})
            expires = (clock.now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(
                201, json={"token": f"ghs_{len(requests)}", "expires_at": expires}
            )

        app = GitHubApp(
            "123", self.private_pem, transport=httpx.MockTransport(handler), clock=clock
        )
        return app, clock, requests

    @staticmethod
    def posts(requests: list[httpx.Request]) -> list[httpx.Request]:
        return [r for r in requests if r.method == "POST"]

    def test_app_jwt_claims(self) -> None:
        app, _, _ = self.make_app()
        # O relógio é fixo (2026), então a validação de iat/exp do pyjwt é desligada.
        options = {"verify_iat": False, "verify_exp": False}
        claims = jwt.decode(app.app_jwt(), self.public_pem, algorithms=["RS256"], options=options)
        self.assertEqual(claims["iss"], "123")
        self.assertEqual(claims["iat"], int(NOW.timestamp()) - 60)
        self.assertEqual(claims["exp"], int(NOW.timestamp()) + 540)

    async def test_installation_token_scoped_to_repo(self) -> None:
        app, _, requests = self.make_app()
        token = await app.installation_token("acme/app")
        self.assertEqual(token, "ghs_2")
        self.assertEqual(requests[0].url.path, "/repos/acme/app/installation")
        post = requests[1]
        self.assertEqual(post.url.path, "/app/installations/9/access_tokens")
        self.assertEqual(
            json.loads(post.content),
            {"repositories": ["app"], "permissions": INSTALLATION_PERMISSIONS},
        )
        self.assertTrue(post.headers["authorization"].startswith("Bearer ey"))

    async def test_token_cached_until_refresh_margin(self) -> None:
        app, clock, requests = self.make_app()
        await app.installation_token("acme/app")
        await app.installation_token("acme/app")
        self.assertEqual(len(self.posts(requests)), 1)
        clock.now = NOW + timedelta(minutes=51)
        await app.installation_token("acme/app")
        self.assertEqual(len(self.posts(requests)), 2)

    async def test_cache_is_per_repo(self) -> None:
        app, _, requests = self.make_app()
        await app.installation_token("acme/app")
        await app.installation_token("acme/api")
        self.assertEqual(len(self.posts(requests)), 2)

    async def test_not_installed_is_non_retryable(self) -> None:
        app, _, _ = self.make_app(installation_status=404)
        with self.assertRaises(ApplicationError) as ctx:
            await app.installation_token("acme/app")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("acme/app", str(ctx.exception))

    async def test_5xx_is_retryable(self) -> None:
        app, _, _ = self.make_app(token_status=502)
        with self.assertRaises(ApplicationError) as ctx:
            await app.installation_token("acme/app")
        self.assertFalse(ctx.exception.non_retryable)
