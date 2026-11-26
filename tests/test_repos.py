import unittest

from software_factory.orchestration.repos import (
    FactoryConfigError,
    linked_repos,
    parse_factory_config,
    pin_url,
    pinned_repos,
    repo_from_url,
)


class TestRepoFromUrl(unittest.TestCase):
    def test_repo_from_url_variants(self) -> None:
        for url in (
            "https://github.com/acme/app",
            "https://github.com/acme/app/",
            "https://github.com/acme/app.git",
            "https://github.com/acme/app/tree/main",
            "http://www.github.com/acme/app",
            "https://GitHub.com/acme/app#readme",
        ):
            with self.subTest(url=url):
                self.assertEqual(repo_from_url(url), "acme/app")

    def test_repo_from_url_rejects(self) -> None:
        for url in (
            "https://github.com/acme",
            "https://github.com/orgs/acme/projects/1",
            "https://github.com/settings/apps",
            "https://gitlab.com/acme/app",
            "https://github.com.evil.io/acme/app",
            "https://github.com/acme/app;rm",
            "https://github.com/acme/..",
            "github.com/acme/app",
            "ftp://github.com/acme/app",
        ):
            with self.subTest(url=url):
                self.assertIsNone(repo_from_url(url))


class TestLinkedRepos(unittest.TestCase):
    def test_linked_repos_dedup_case_insensitive(self) -> None:
        self.assertEqual(
            linked_repos(
                [
                    "https://github.com/acme/app",
                    "https://docs.google.com/x",
                    "https://github.com/Acme/App/issues",
                ]
            ),
            ["acme/app"],
        )

    def test_linked_repos_two_repos(self) -> None:
        self.assertEqual(
            linked_repos(
                [
                    "https://github.com/acme/app",
                    "https://github.com/acme/api",
                    "https://github.com/Acme/App/pull/3",
                ]
            ),
            ["acme/app", "acme/api"],
        )


class TestPinnedRepos(unittest.TestCase):
    def test_pinned_repos_only_factory_tree(self) -> None:
        self.assertEqual(
            pinned_repos(
                [
                    "https://github.com/acme/app/tree/factory/ENG-12",
                    "https://github.com/acme/app/pull/7",
                    "https://github.com/acme/api/tree/main",
                ]
            ),
            ["acme/app"],
        )

    def test_pin_url(self) -> None:
        self.assertEqual(
            pin_url("acme/app", "ENG-12"),
            "https://github.com/acme/app/tree/factory/ENG-12",
        )
        self.assertEqual(pinned_repos([pin_url("acme/app", "ENG-12")]), ["acme/app"])


class TestParseFactoryConfig(unittest.TestCase):
    def test_parse_factory_config(self) -> None:
        self.assertEqual(
            parse_factory_config('test_command = "  python -m pytest -q "\n'),
            "python -m pytest -q",
        )
        self.assertEqual(
            parse_factory_config("test_command = 'npm test -- --grep \"a b\"'"),
            'npm test -- --grep "a b"',
        )

    def test_parse_factory_config_errors(self) -> None:
        for text in ("", "test_command = ''", "test_command = 3", "test_command = [", "outro = 'x'"):
            with self.subTest(text=text), self.assertRaises(FactoryConfigError) as ctx:
                parse_factory_config(text)
            self.assertIn(".factory.toml", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
