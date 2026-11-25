import unittest

from temporalio.exceptions import ApplicationError

from software_factory.orchestration.models import RepoTarget
from software_factory.orchestration.sessions import SessionWorkflow, format_question


class FormatQuestionTests(unittest.TestCase):
    def test_format_question_names_state(self) -> None:
        text = format_question("Q?", "In Review")
        self.assertIn("Q?", text)
        self.assertIn("Responda **nesta thread**", text)
        self.assertIn("Para parar, mova a issue para fora de In Review.", text)


class SessionTargetTests(unittest.TestCase):
    def test_target_before_resolution_fails_without_retry(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            SessionWorkflow().target
        self.assertTrue(ctx.exception.non_retryable)

    def test_target_after_resolution(self) -> None:
        session = SessionWorkflow()
        session._target = RepoTarget("acme/app", "pytest")
        self.assertEqual(session.target, RepoTarget("acme/app", "pytest"))


if __name__ == "__main__":
    unittest.main()
