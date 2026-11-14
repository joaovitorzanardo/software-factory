import unittest

from software_factory.orchestration.sessions import format_question


class FormatQuestionTests(unittest.TestCase):
    def test_format_question_names_state(self) -> None:
        text = format_question("Q?", "In Review")
        self.assertIn("Q?", text)
        self.assertIn("Responda **nesta thread**", text)
        self.assertIn("Para parar, mova a issue para fora de In Review.", text)


if __name__ == "__main__":
    unittest.main()
