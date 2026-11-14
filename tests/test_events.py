import unittest

from software_factory.config import DEFAULT_TRIGGER_LABELS
from software_factory.linear.events import (
    CommentReply,
    parse_issue,
    parse_reply,
    should_implement,
    should_process,
)
from software_factory.orchestration.models import HumanAnswer
from tests.payloads import BUG, DOCS, FEATURE, comment_payload, issue_payload


def process(payload) -> bool:
    return should_process(payload, DEFAULT_TRIGGER_LABELS)


class TestShouldProcess(unittest.TestCase):
    def test_create_with_feature(self) -> None:
        self.assertTrue(process(issue_payload(labels=[FEATURE])))

    def test_create_with_bug_lowercase(self) -> None:
        self.assertTrue(process(issue_payload(labels=[BUG, DOCS])))

    def test_create_without_trigger_label(self) -> None:
        self.assertFalse(process(issue_payload(labels=[DOCS])))
        self.assertFalse(process(issue_payload(labels=[])))

    def test_update_adding_trigger_label(self) -> None:
        payload = issue_payload(
            "update", labels=[DOCS, FEATURE], updated_from={"labelIds": [DOCS["id"]]}
        )
        self.assertTrue(process(payload))

    def test_update_label_already_present(self) -> None:
        payload = issue_payload(
            "update", labels=[FEATURE, DOCS], updated_from={"labelIds": [FEATURE["id"]]}
        )
        self.assertFalse(process(payload))

    def test_update_other_field(self) -> None:
        payload = issue_payload("update", labels=[FEATURE], updated_from={"title": "Antigo"})
        self.assertFalse(process(payload))

    def test_remove(self) -> None:
        self.assertFalse(process(issue_payload("remove", labels=[FEATURE])))

    def test_non_issue_type(self) -> None:
        self.assertFalse(process(issue_payload(labels=[FEATURE], type_="Comment")))


IN_PROGRESS = {"id": "s1", "name": "In Progress"}


def implement(payload) -> bool:
    return should_implement(payload, DEFAULT_TRIGGER_LABELS)


class TestShouldImplement(unittest.TestCase):
    def test_moved_to_in_progress_with_label(self) -> None:
        upper = {**FEATURE, "name": "FEATURE"}
        payload = issue_payload(
            "update", labels=[upper], updated_from={"stateId": "s-old"}, state=IN_PROGRESS
        )
        self.assertTrue(implement(payload))
        lower = {"id": "s1", "name": "in progress"}
        payload = issue_payload(
            "update", labels=[BUG], updated_from={"stateId": "s-old"}, state=lower
        )
        self.assertTrue(implement(payload))

    def test_without_trigger_label(self) -> None:
        payload = issue_payload(
            "update", labels=[DOCS], updated_from={"stateId": "s-old"}, state=IN_PROGRESS
        )
        self.assertFalse(implement(payload))

    def test_other_state(self) -> None:
        payload = issue_payload(
            "update",
            labels=[FEATURE],
            updated_from={"stateId": "s-old"},
            state={"id": "s2", "name": "In Review"},
        )
        self.assertFalse(implement(payload))

    def test_state_not_changed(self) -> None:
        payload = issue_payload(
            "update", labels=[FEATURE], updated_from={"title": "Antigo"}, state=IN_PROGRESS
        )
        self.assertFalse(implement(payload))

    def test_create(self) -> None:
        self.assertFalse(implement(issue_payload(labels=[FEATURE], state=IN_PROGRESS)))

    def test_non_issue_type(self) -> None:
        payload = issue_payload(
            "update",
            labels=[FEATURE],
            updated_from={"stateId": "s-old"},
            state=IN_PROGRESS,
            type_="Comment",
        )
        self.assertFalse(implement(payload))

    def test_label_added_and_moved_is_planning(self) -> None:
        payload = issue_payload(
            "update",
            labels=[FEATURE],
            updated_from={"labelIds": [], "stateId": "s-old"},
            state=IN_PROGRESS,
        )
        self.assertTrue(process(payload))
        self.assertTrue(implement(payload))


class TestParseIssue(unittest.TestCase):
    def test_parse(self) -> None:
        issue = parse_issue(issue_payload(labels=[FEATURE, DOCS]))
        self.assertEqual(issue.id, "issue-uuid")
        self.assertEqual(issue.identifier, "ENG-12")
        self.assertEqual(issue.title, "Título")
        self.assertEqual(issue.team_key, "ENG")
        self.assertEqual(issue.labels, ("Feature", "Docs"))
        self.assertEqual(issue.url, "https://linear.app/acme/issue/ENG-12/titulo")


if __name__ == "__main__":
    unittest.main()


class TestParseReply(unittest.TestCase):
    def test_parse_reply(self) -> None:
        self.assertEqual(
            parse_reply(comment_payload()),
            CommentReply("issue-uuid", HumanAnswer("question-1", "comment-1", "Use bcrypt")),
        )

    def test_ignores_top_level_comment(self) -> None:
        self.assertIsNone(parse_reply(comment_payload(parent_id=None)))

    def test_ignores_empty_body(self) -> None:
        self.assertIsNone(parse_reply(comment_payload(body="")))

    def test_ignores_update_and_issue_events(self) -> None:
        self.assertIsNone(parse_reply(comment_payload(action="update")))
        self.assertIsNone(parse_reply(issue_payload(labels=[FEATURE])))
