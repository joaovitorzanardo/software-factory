import json
import unittest
from typing import Any

import httpx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.linear.client import LinearClient
from software_factory.orchestration.linear_activities import LinearActivities
from software_factory.orchestration.models import (
    CommentRequest,
    CreateIssueRequest,
    FollowUpIssue,
    MoveIssueRequest,
)

TEAM_STATES = [
    {"id": "state-todo", "name": "To Do"},
    {"id": "state-planning", "name": "In Planning"},
    {"id": "state-progress", "name": "In Progress"},
    {"id": "state-done", "name": "Done"},
]


def issue_response(state_name: str, states: list[dict[str, str]] = TEAM_STATES) -> dict[str, Any]:
    current = next((s for s in states if s["name"].lower() == state_name.lower()), None)
    state = {"id": current["id"] if current else "state-other", "name": state_name}
    return {"data": {"issue": {"state": state, "team": {"states": {"nodes": states}}}}}


UPDATE_OK = {"data": {"issueUpdate": {"success": True}}}


class TestMoveIssue(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.responses.pop(0))

        client = LinearClient("lin-key", transport=httpx.MockTransport(handler))
        self.activities = LinearActivities(client)

    async def run_move(self, request: MoveIssueRequest, *responses: dict[str, Any]) -> bool:
        self.responses.extend(responses)
        return await ActivityEnvironment().run(self.activities.move_issue, request)

    def variables(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)["variables"]

    async def test_moves_from_todo_to_in_planning(self) -> None:
        moved = await self.run_move(
            MoveIssueRequest("issue-1", to_state="In Planning", from_state="To Do"),
            issue_response("To Do"),
            UPDATE_OK,
        )
        self.assertTrue(moved)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.variables(0), {"id": "issue-1"})
        self.assertEqual(self.variables(1), {"id": "issue-1", "stateId": "state-planning"})

    async def test_skips_when_not_in_from_state(self) -> None:
        moved = await self.run_move(
            MoveIssueRequest("issue-1", to_state="In Planning", from_state="To Do"),
            issue_response("Done"),
        )
        self.assertFalse(moved)
        self.assertEqual(len(self.requests), 1)

    async def test_already_in_target_state_is_noop(self) -> None:
        moved = await self.run_move(
            MoveIssueRequest("issue-1", to_state="In Progress"), issue_response("in progress")
        )
        self.assertTrue(moved)
        self.assertEqual(len(self.requests), 1)

    async def test_moves_without_guard(self) -> None:
        moved = await self.run_move(
            MoveIssueRequest("issue-1", to_state="In Progress"),
            issue_response("In Planning"),
            UPDATE_OK,
        )
        self.assertTrue(moved)
        self.assertEqual(self.variables(1), {"id": "issue-1", "stateId": "state-progress"})

    async def test_unknown_target_state_is_non_retryable(self) -> None:
        states = [s for s in TEAM_STATES if s["name"] != "In Planning"]
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_move(
                MoveIssueRequest("issue-1", to_state="In Planning", from_state="To Do"),
                issue_response("To Do", states),
            )
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("In Planning", str(ctx.exception))

    async def test_missing_required_state_fails_even_when_guard_skips(self) -> None:
        states = [s for s in TEAM_STATES if s["name"] != "In Progress"]
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_move(
                MoveIssueRequest(
                    "issue-1",
                    to_state="In Planning",
                    from_state="To Do",
                    require_states=("In Progress",),
                ),
                issue_response("Done", states),
            )
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("In Progress", str(ctx.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_missing_issue_is_non_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_move(
                MoveIssueRequest("issue-1", to_state="In Planning"), {"data": {"issue": None}}
            )
        self.assertTrue(ctx.exception.non_retryable)

    async def test_unsuccessful_update_raises(self) -> None:
        with self.assertRaises(ApplicationError):
            await self.run_move(
                MoveIssueRequest("issue-1", to_state="In Progress"),
                issue_response("In Planning"),
                {"data": {"issueUpdate": {"success": False}}},
            )


class TestPostComment(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.responses.pop(0))

        client = LinearClient("lin-key", transport=httpx.MockTransport(handler))
        self.activities = LinearActivities(client)

    async def run_post(self, request: CommentRequest, *responses: dict[str, Any]) -> str:
        self.responses.extend(responses)
        return await ActivityEnvironment().run(self.activities.post_comment, request)

    def variables(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)["variables"]

    async def test_creates_comment(self) -> None:
        result = await self.run_post(
            CommentRequest("issue-1", "c-1", "Pergunta?"),
            {"data": {"issue": {"comments": {"nodes": []}}}},
            {"data": {"commentCreate": {"success": True, "comment": {"id": "c-1"}}}},
        )
        self.assertEqual(result, "c-1")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.variables(0), {"issueId": "issue-1", "commentId": "c-1"})
        self.assertEqual(
            self.variables(1),
            {"input": {"id": "c-1", "issueId": "issue-1", "body": "Pergunta?"}},
        )

    async def test_retry_after_create_is_noop(self) -> None:
        result = await self.run_post(
            CommentRequest("issue-1", "c-1", "Pergunta?"),
            {"data": {"issue": {"comments": {"nodes": [{"id": "c-1"}]}}}},
        )
        self.assertEqual(result, "c-1")
        self.assertEqual(len(self.requests), 1)

    async def test_missing_issue_is_non_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_post(
                CommentRequest("issue-1", "c-1", "Pergunta?"), {"data": {"issue": None}}
            )
        self.assertTrue(ctx.exception.non_retryable)
        self.assertEqual(len(self.requests), 1)

    async def test_unsuccessful_create_is_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_post(
                CommentRequest("issue-1", "c-1", "Pergunta?"),
                {"data": {"issue": {"comments": {"nodes": []}}}},
                {"data": {"commentCreate": {"success": False, "comment": None}}},
            )
        self.assertFalse(ctx.exception.non_retryable)


class TestCreateIssue(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.responses.pop(0))

        client = LinearClient("lin-key", transport=httpx.MockTransport(handler))
        self.activities = LinearActivities(client)
        self.request = CreateIssueRequest("issue-1", "new-1", "Follow-up", "Detalhes")

    async def run_create(self, *responses: dict[str, Any]) -> FollowUpIssue:
        self.responses.extend(responses)
        return await ActivityEnvironment().run(self.activities.create_issue, self.request)

    def variables(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)["variables"]

    def create_responses(self) -> tuple[dict[str, Any], ...]:
        return (
            {"data": {"issues": {"nodes": []}}},
            {"data": {"issue": {"team": {"id": "team-1"}}}},
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"identifier": "SW-9", "url": "https://linear.app/x/SW-9"},
                    }
                }
            },
        )

    async def test_creates_issue_in_origin_team(self) -> None:
        result = await self.run_create(*self.create_responses())
        self.assertEqual(result, FollowUpIssue("SW-9", "https://linear.app/x/SW-9"))
        self.assertEqual(len(self.requests), 3)
        issue_input = self.variables(2)["input"]
        self.assertEqual(issue_input["id"], "new-1")
        self.assertEqual(issue_input["teamId"], "team-1")
        self.assertEqual(issue_input["title"], "Follow-up")
        self.assertEqual(issue_input["description"], "Detalhes")

    async def test_creates_issue_without_labels(self) -> None:
        await self.run_create(*self.create_responses())
        self.assertNotIn("labelIds", self.variables(2)["input"])

    async def test_retry_after_create_is_noop(self) -> None:
        result = await self.run_create(
            {"data": {"issues": {"nodes": [{"identifier": "SW-9", "url": "https://u/SW-9"}]}}}
        )
        self.assertEqual(result, FollowUpIssue("SW-9", "https://u/SW-9"))
        self.assertEqual(len(self.requests), 1)

    async def test_missing_origin_is_non_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_create(
                {"data": {"issues": {"nodes": []}}}, {"data": {"issue": None}}
            )
        self.assertTrue(ctx.exception.non_retryable)
        self.assertEqual(len(self.requests), 2)

    async def test_unsuccessful_create_is_retryable(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_create(
                {"data": {"issues": {"nodes": []}}},
                {"data": {"issue": {"team": {"id": "team-1"}}}},
                {"data": {"issueCreate": {"success": False, "issue": None}}},
            )
        self.assertFalse(ctx.exception.non_retryable)


if __name__ == "__main__":
    unittest.main()
