import json
import unittest

import httpx
from temporalio.exceptions import ApplicationError

from software_factory.github.client import GitHubClient


def make_client(response: httpx.Response, requests: list[httpx.Request]) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    return GitHubClient("gh-token", "acme/app", transport=httpx.MockTransport(handler))


class TestGitHubClient(unittest.IsolatedAsyncioTestCase):
    async def test_default_branch(self) -> None:
        requests: list[httpx.Request] = []
        client = make_client(httpx.Response(200, json={"default_branch": "main"}), requests)
        self.assertEqual(await client.default_branch(), "main")
        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), "https://api.github.com/repos/acme/app")
        self.assertEqual(request.headers["Authorization"], "Bearer gh-token")
        self.assertEqual(request.headers["X-GitHub-Api-Version"], "2022-11-28")
        self.assertEqual(request.headers["Accept"], "application/vnd.github+json")

    async def test_get_file_returns_text(self) -> None:
        requests: list[httpx.Request] = []
        client = make_client(httpx.Response(200, text="# Plano"), requests)
        content = await client.get_file("docs/superpowers/plans/ENG-12.md", "factory/ENG-12")
        self.assertEqual(content, "# Plano")
        request = requests[0]
        self.assertEqual(
            request.url.path, "/repos/acme/app/contents/docs/superpowers/plans/ENG-12.md"
        )
        self.assertEqual(request.url.params["ref"], "factory/ENG-12")
        self.assertEqual(request.headers["Accept"], "application/vnd.github.raw+json")

    async def test_get_file_missing_returns_none(self) -> None:
        client = make_client(httpx.Response(404, json={"message": "Not Found"}), [])
        self.assertIsNone(await client.get_file("a.md", "factory/ENG-12"))

    async def test_find_open_pull(self) -> None:
        requests: list[httpx.Request] = []
        url = "https://github.com/acme/app/pull/7"
        client = make_client(httpx.Response(200, json=[{"html_url": url}]), requests)
        self.assertEqual(await client.find_open_pull("factory/ENG-12"), url)
        request = requests[0]
        self.assertEqual(request.url.path, "/repos/acme/app/pulls")
        self.assertEqual(request.url.params["head"], "acme:factory/ENG-12")
        self.assertEqual(request.url.params["state"], "open")

        client = make_client(httpx.Response(200, json=[]), [])
        self.assertIsNone(await client.find_open_pull("factory/ENG-12"))

    async def test_create_pull(self) -> None:
        requests: list[httpx.Request] = []
        url = "https://github.com/acme/app/pull/8"
        client = make_client(httpx.Response(201, json={"html_url": url}), requests)
        result = await client.create_pull("ENG-12: t", "factory/ENG-12", "main", "corpo")
        self.assertEqual(result, url)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/repos/acme/app/pulls")
        self.assertEqual(
            json.loads(request.content),
            {"title": "ENG-12: t", "head": "factory/ENG-12", "base": "main", "body": "corpo"},
        )

    async def error(self, response: httpx.Response, call: str = "default_branch") -> ApplicationError:
        client = make_client(response, [])
        with self.assertRaises(ApplicationError) as ctx:
            if call == "create_pull":
                await client.create_pull("t", "factory/ENG-12", "main", "b")
            else:
                await client.default_branch()
        return ctx.exception

    async def test_rate_limited_is_retryable(self) -> None:
        error = await self.error(httpx.Response(429, json={"message": "slow down"}))
        self.assertFalse(error.non_retryable)
        error = await self.error(
            httpx.Response(
                403, json={"message": "rate limit"}, headers={"x-ratelimit-remaining": "0"}
            )
        )
        self.assertFalse(error.non_retryable)

    async def test_server_error_is_retryable(self) -> None:
        error = await self.error(httpx.Response(502, text="Bad Gateway"))
        self.assertFalse(error.non_retryable)

    async def test_auth_error_is_not_retryable(self) -> None:
        for status in (401, 404, 403):
            with self.subTest(status=status):
                error = await self.error(httpx.Response(status, json={"message": "nope"}))
                self.assertTrue(error.non_retryable)

    async def test_unprocessable_is_not_retryable(self) -> None:
        error = await self.error(
            httpx.Response(422, json={"message": "Validation Failed"}), call="create_pull"
        )
        self.assertTrue(error.non_retryable)
        self.assertIn("Validation Failed", str(error))
        self.assertIn("422", str(error))


if __name__ == "__main__":
    unittest.main()
