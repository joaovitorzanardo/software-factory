import asyncio
import json
import unittest
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.github.client import static_token
from software_factory.orchestration.activities import (
    IMPLEMENT_COMMAND_TIMEOUT_S,
    REPO_DIR,
    SKILLS_DEST_DIR,
    SandboxActivities,
)
from software_factory.orchestration.models import (
    ReviewFinding,
    ReviewFixRequest,
    ReviewReport,
    ReviewRequest,
)
from software_factory.orchestration.review_activities import (
    CORRECTNESS_PROMPT_PATH,
    REVIEW_FIX_PROMPT_PATH,
    REVIEW_LENS_TIMEOUT_S,
    REVIEW_TRIAGE_TIMEOUT_S,
    SECURITY_PROMPT_PATH,
    TRIAGE_PROMPT_PATH,
    ReviewActivities,
)
from software_factory.orchestration.review_prompts import (
    CORRECTNESS_PATH,
    REVIEW_DIR,
    REVIEW_JSON_PATH,
    SECURITY_PATH,
)
from tests.test_activities import (
    ISSUE,
    SETTINGS,
    USAGE_EVENT,
    USAGE_RESET,
    FakeNotifier,
    FakeSandbox,
    stream,
)

BASE_SHA = "b" * 40
FINDING = {
    "lens": "security",
    "severity": "important",
    "file": "app/login.py",
    "line": 7,
    "title": "Entrada sem validação",
    "detail": "O e-mail vai direto para a query.",
    "decision": "fix",
    "reason": "Introduzido pela PR.",
}
REVIEW_JSON = json.dumps({"summary": "Uma correção.", "findings": [FINDING]})


class TestReviewActivities(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeSandbox.reset()
        sandbox = SandboxActivities(SETTINGS, static_token("gh-token"), sandbox_cls=FakeSandbox)
        self.activities = ReviewActivities(sandbox)
        self.env = ActivityEnvironment()
        self.heartbeats: list[Any] = []
        self.env.on_heartbeat = lambda *details: self.heartbeats.append(details)
        self.request = ReviewRequest(sandbox_id="sbx-1", issue=ISSUE, base_sha=BASE_SHA)
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "success", "result": "Relatório salvo."}
        )

    def claude_reviews(self, review_json: str | None = REVIEW_JSON) -> list[str]:
        """Lentes gravam os relatórios e a triagem grava o JSON; devolve a ordem dos eventos."""
        files = FakeSandbox.instance.files
        events: list[str] = []
        both_started = asyncio.Event()
        started: set[str] = set()

        async def hook(cmd: str) -> None:
            for name, prompt, out in (
                ("correctness", CORRECTNESS_PROMPT_PATH, CORRECTNESS_PATH),
                ("security", SECURITY_PROMPT_PATH, SECURITY_PATH),
            ):
                if prompt in cmd:
                    events.append(f"start:{name}")
                    started.add(name)
                    if len(started) == 2:
                        both_started.set()
                    # Sequencial trava aqui: a outra lente nunca começa.
                    await asyncio.wait_for(both_started.wait(), timeout=1)
                    files.written[out] = f"# {name}"
                    events.append(f"end:{name}")
            if TRIAGE_PROMPT_PATH in cmd:
                events.append("triage")
                if review_json is not None:
                    files.written[REVIEW_JSON_PATH] = review_json

        FakeSandbox.instance.commands.hook = hook
        return events

    async def test_review_pull_notifies_usage(self) -> None:
        notifier = FakeNotifier()
        sandbox = SandboxActivities(SETTINGS, static_token("gh-token"), sandbox_cls=FakeSandbox, usage_notifier=notifier)
        activities = ReviewActivities(sandbox)
        self.claude_reviews()
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Relatório salvo."}
        )
        await self.env.run(activities.review_pull, self.request)
        # Um watcher por sessão do Claude; o dedup entre elas é do Temporal (id por janela).
        self.assertGreaterEqual(len(notifier.alerts), 1)
        self.assertEqual({alert.resets_at for alert in notifier.alerts}, {USAGE_RESET})

    async def test_review_pull_runs_lenses_in_parallel_then_triage(self) -> None:
        events = self.claude_reviews()
        report = await self.env.run(self.activities.review_pull, self.request)
        self.assertEqual(
            report,
            ReviewReport(summary="Uma correção.", findings=(ReviewFinding(**FINDING),)),
        )
        self.assertEqual(set(events[:2]), {"start:correctness", "start:security"})
        self.assertEqual(events[-1], "triage")
        runs = FakeSandbox.instance.commands.runs
        cmds = [r["cmd"] for r in runs]
        reset = next(i for i, c in enumerate(cmds) if f"rm -rf {REVIEW_DIR}" in c)
        fetch = next(i for i, c in enumerate(cmds) if "git fetch --depth 1" in c)
        first_lens = next(i for i, c in enumerate(cmds) if CORRECTNESS_PROMPT_PATH in c)
        self.assertLess(reset, fetch)
        self.assertLess(fetch, first_lens)
        lens_runs = [
            r for r in runs if "claude -p" in r["cmd"] and TRIAGE_PROMPT_PATH not in r["cmd"]
        ]
        self.assertEqual(len(lens_runs), 2)
        for run in lens_runs:
            self.assertEqual(run["timeout"], REVIEW_LENS_TIMEOUT_S)
            self.assertEqual(run["cwd"], REPO_DIR)
            self.assertIn('--allowedTools "Read,Grep,Glob,Skill,Bash,Write"', run["cmd"])
        (triage,) = [r for r in runs if TRIAGE_PROMPT_PATH in r["cmd"]]
        self.assertEqual(triage["timeout"], REVIEW_TRIAGE_TIMEOUT_S)
        self.assertIn('--allowedTools "Read,Grep,Glob,Skill,Bash,Write"', triage["cmd"])
        written = FakeSandbox.instance.files.written
        self.assertIn(f"{SKILLS_DEST_DIR}/requesting-code-review/code-reviewer.md", written)
        self.assertIn(f"{SKILLS_DEST_DIR}/receiving-code-review/SKILL.md", written)
        self.assertTrue(self.heartbeats)

    async def test_review_pull_fetches_base_with_token_env_only(self) -> None:
        self.claude_reviews()
        await self.env.run(self.activities.review_pull, self.request)
        runs = FakeSandbox.instance.commands.runs
        (fetch,) = [r for r in runs if "git fetch" in r["cmd"]]
        self.assertIn("github.com/acme/app.git", fetch["cmd"])
        self.assertIn(BASE_SHA, fetch["cmd"])
        self.assertEqual(fetch["envs"], {"GITHUB_TOKEN": "gh-token"})
        self.assertEqual(fetch["cwd"], REPO_DIR)
        for run in runs:
            self.assertNotIn("gh-token", run["cmd"])
            if run is not fetch:
                self.assertNotIn("GITHUB_TOKEN", str(run.get("envs", "")))
        written = FakeSandbox.instance.files.written
        for path in (CORRECTNESS_PROMPT_PATH, SECURITY_PROMPT_PATH, TRIAGE_PROMPT_PATH):
            self.assertIn(path, written)
            self.assertNotIn("gh-token", written[path])

    async def test_review_pull_flags_sensitive_files_before_claude(self) -> None:
        self.claude_reviews()
        FakeSandbox.instance.commands.responses = {
            "git diff --name-only": "src/app.py\nCLAUDE.md\n.github/workflows/ci.yml\n"
        }
        report = await self.env.run(self.activities.review_pull, self.request)
        self.assertEqual(report.sensitive_files, ("CLAUDE.md", ".github/workflows/ci.yml"))
        runs = FakeSandbox.instance.commands.runs
        cmds = [r["cmd"] for r in runs]
        diff = next(i for i, c in enumerate(cmds) if "git diff --name-only" in c)
        fetch = next(i for i, c in enumerate(cmds) if "git fetch --depth 1" in c)
        first_claude = next(i for i, c in enumerate(cmds) if "claude -p" in c)
        self.assertLess(fetch, diff)
        self.assertLess(diff, first_claude)
        self.assertIn(f"git diff --name-only {BASE_SHA} HEAD", cmds[diff])
        self.assertEqual(runs[diff]["cwd"], REPO_DIR)

    async def test_review_pull_without_sensitive_files(self) -> None:
        self.claude_reviews()
        FakeSandbox.instance.commands.responses = {"git diff --name-only": "src/app.py\n"}
        report = await self.env.run(self.activities.review_pull, self.request)
        self.assertEqual(report.sensitive_files, ())

    async def test_review_pull_rejects_unsafe_base_sha(self) -> None:
        self.claude_reviews()
        request = ReviewRequest(sandbox_id="sbx-1", issue=ISSUE, base_sha="main; rm -rf /")
        with self.assertRaises(ApplicationError) as ctx:
            await self.env.run(self.activities.review_pull, request)
        self.assertTrue(ctx.exception.non_retryable)
        self.assertEqual(FakeSandbox.instance.commands.runs, [])

    async def test_review_pull_missing_json_raises(self) -> None:
        self.claude_reviews(review_json=None)
        with self.assertRaises(ApplicationError):
            await self.env.run(self.activities.review_pull, self.request)

    async def test_review_pull_lens_failure_raises(self) -> None:
        self.claude_reviews()
        FakeSandbox.instance.commands.responses = {
            SECURITY_PROMPT_PATH: stream({"type": "result", "subtype": "error_max_turns"})
        }
        with self.assertRaises(ApplicationError):
            await self.env.run(self.activities.review_pull, self.request)
        cmds = [r["cmd"] for r in FakeSandbox.instance.commands.runs]
        self.assertFalse(any(TRIAGE_PROMPT_PATH in c for c in cmds))

    async def test_implement_review_fix_returns_notes(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "success", "result": "Corrigi o achado 1."}
        )
        request = ReviewFixRequest(
            sandbox_id="sbx-1", issue=ISSUE, round=1, findings=(ReviewFinding(**FINDING),)
        )
        notes = await self.env.run(self.activities.implement_review_fix, request)
        self.assertEqual(notes, "Corrigi o achado 1.")
        written = FakeSandbox.instance.files.written
        self.assertIn("Entrada sem validação", written[REVIEW_FIX_PROMPT_PATH])
        self.assertIn(f"{SKILLS_DEST_DIR}/receiving-code-review/SKILL.md", written)
        (run,) = FakeSandbox.instance.commands.runs
        self.assertIn(REVIEW_FIX_PROMPT_PATH, run["cmd"])
        self.assertIn('--allowedTools "Read,Grep,Glob,Skill,Edit,Write,Bash"', run["cmd"])
        self.assertEqual(run["timeout"], IMPLEMENT_COMMAND_TIMEOUT_S)

    async def test_install_skills_copies_review_skills(self) -> None:
        from software_factory.orchestration.activities import install_skills

        await install_skills(FakeSandbox.instance)
        written = FakeSandbox.instance.files.written
        for path in (
            "requesting-code-review/SKILL.md",
            "requesting-code-review/code-reviewer.md",
            "requesting-code-review/LICENSE",
            "receiving-code-review/SKILL.md",
            "receiving-code-review/LICENSE",
        ):
            self.assertIn(f"{SKILLS_DEST_DIR}/{path}", written)


if __name__ == "__main__":
    unittest.main()
