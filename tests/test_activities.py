import json
import os
import unittest
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from unittest import mock

from e2b import CommandExitException, FileNotFoundException
from e2b.retry import resolve_idempotency_key
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from software_factory.config import FactorySettings
from software_factory.orchestration.activities import (
    ANALYSIS_PATH,
    FIX_DIR,
    FIX_PLAN_PATH,
    FIX_PROMPT_PATH,
    QUESTION_PATH,
    REPO_DIR,
    SKILLS_DEST_DIR,
    SandboxActivities,
    build_analyze_prompt,
    build_fix_prompt,
    build_implement_prompt,
    build_plan_prompt,
    plan_path,
)
from software_factory.orchestration.models import (
    AnalyzeRequest,
    CloneBranchRequest,
    Clarification,
    CommitImplementationRequest,
    CommitPlanRequest,
    FixAnalysis,
    FixAttempt,
    FixRequest,
    ImplementRequest,
    PlanIssueInput,
    PlanRequest,
    TestRunResult,
)

SETTINGS = FactorySettings(
    e2b_api_key="e2b-key",
    anthropic_api_key="anthropic-key",
    github_token="gh-token",
    target_repo="acme/app",
    linear_api_key="lin-key",
    test_command="python -m pytest",
)

ISSUE = PlanIssueInput(
    issue_id="issue-1",
    identifier="ENG-12",
    title="Adicionar login",
    description="Login com e-mail e senha.",
    url="https://linear.app/acme/issue/ENG-12",
)


@dataclass
class FakeResult:
    stdout: str
    exit_code: int = 0


@dataclass
class FakeCommands:
    stdout: str = ""
    # substring do comando -> stdout; a primeira chave contida no comando vence.
    responses: dict[str, str] = field(default_factory=dict)
    # substring do comando -> (stdout, stderr, exit_code): levanta CommandExitException.
    fail: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    on_run: Callable[[str], None] | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, cmd: str, **kwargs: Any) -> FakeResult:
        self.runs.append({"cmd": cmd, **kwargs})
        failure = next((out for key, out in self.fail.items() if key in cmd), None)
        if failure:
            stdout, stderr, exit_code = failure
            raise CommandExitException(
                stderr=stderr, stdout=stdout, exit_code=exit_code, error=None
            )
        stdout = next((out for key, out in self.responses.items() if key in cmd), self.stdout)
        on_stdout = kwargs.get("on_stdout")
        if on_stdout and stdout:
            on_stdout(stdout)
        if self.on_run:
            self.on_run(cmd)
        return FakeResult(stdout=stdout)


@dataclass
class FakeFiles:
    existing: set[str] = field(default_factory=set)
    written: dict[str, str] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)

    async def exists(self, path: str, **kwargs: Any) -> bool:
        return path in self.existing or path in self.written

    async def write(self, path: str, data: str, **kwargs: Any) -> None:
        self.written[path] = data

    async def read(self, path: str, **kwargs: Any) -> str:
        try:
            return self.written[path]
        except KeyError:
            raise FileNotFoundException(path) from None

    async def remove(self, path: str, **kwargs: Any) -> None:
        self.written.pop(path, None)
        self.existing.discard(path)
        self.removed.append(path)


class FakeSandbox:
    """Imita o AsyncSandbox do E2B; o estado fica na classe para o teste inspecionar."""

    instance: "FakeSandbox"
    create_calls: list[dict[str, Any]]
    killed: list[str]

    def __init__(self, sandbox_id: str = "sbx-1") -> None:
        self.sandbox_id = sandbox_id
        self.commands = FakeCommands()
        self.files = FakeFiles()

    @classmethod
    def reset(cls) -> None:
        cls.instance = cls()
        cls.create_calls = []
        cls.killed = []

    @classmethod
    async def create(cls, **kwargs: Any) -> "FakeSandbox":
        cls.create_calls.append(kwargs)
        return cls.instance

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs: Any) -> "FakeSandbox":
        assert sandbox_id == cls.instance.sandbox_id
        return cls.instance

    @classmethod
    async def kill(cls, sandbox_id: str, **kwargs: Any) -> bool:
        cls.killed.append(sandbox_id)
        return True


PLAN_FILE = f"{REPO_DIR}/docs/superpowers/plans/ENG-12.md"
SKILL_DEST = f"{SKILLS_DEST_DIR}/writing-plans/SKILL.md"


def stream(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestFactorySettings(unittest.TestCase):
    def test_requires_exactly_one_claude_auth(self) -> None:
        with self.assertRaises(RuntimeError):
            replace(SETTINGS, anthropic_api_key=None)
        with self.assertRaises(RuntimeError):
            replace(SETTINGS, claude_code_oauth_token="oat-token")

    def test_from_env_requires_linear_api_key(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_TOKEN": "gh-token",
            "TARGET_REPO": "acme/app",
            "TEST_COMMAND": "make test",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                FactorySettings.from_env()
        with mock.patch.dict(os.environ, {**env, "LINEAR_API_KEY": "lin-key"}, clear=True):
            self.assertEqual(FactorySettings.from_env().linear_api_key, "lin-key")

    def test_from_env_requires_test_command(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_TOKEN": "gh-token",
            "TARGET_REPO": "acme/app",
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                FactorySettings.from_env()
        with mock.patch.dict(os.environ, {**env, "TEST_COMMAND": "make test"}, clear=True):
            self.assertEqual(FactorySettings.from_env().test_command, "make test")


class TestBuildPlanPrompt(unittest.TestCase):
    def test_includes_issue_fields(self) -> None:
        prompt = build_plan_prompt(ISSUE)
        self.assertIn("ENG-12", prompt)
        self.assertIn("Adicionar login", prompt)
        self.assertIn("Login com e-mail e senha.", prompt)
        self.assertIn(ISSUE.url, prompt)

    def test_missing_description(self) -> None:
        issue = PlanIssueInput("issue-1", "ENG-12", "Título", None, None)
        self.assertIn("sem descrição", build_plan_prompt(issue))

    def test_prompt_uses_skill_and_path(self) -> None:
        prompt = build_plan_prompt(ISSUE)
        self.assertIn("writing-plans", prompt)
        self.assertIn("docs/superpowers/plans/ENG-12.md", prompt)


class TestBuildImplementPrompt(unittest.TestCase):
    def test_implement_prompt(self) -> None:
        prompt = build_implement_prompt(ISSUE, "python -m pytest")
        self.assertIn("ENG-12", prompt)
        self.assertIn("Adicionar login", prompt)
        self.assertIn("Login com e-mail e senha.", prompt)
        self.assertIn("docs/superpowers/plans/ENG-12.md", prompt)
        self.assertIn("python -m pytest", prompt)
        self.assertIn("push", prompt)

    def test_missing_description(self) -> None:
        issue = PlanIssueInput("issue-1", "ENG-12", "Título", None, None)
        self.assertIn("sem descrição", build_implement_prompt(issue, "make test"))


class TestFixPrompts(unittest.TestCase):
    def test_analyze_prompt(self) -> None:
        request = AnalyzeRequest(
            "sbx-1", ISSUE, test_output="FAILED test_login",
            attempts=(FixAttempt(1, "faltava hash", "FAILED test_hash"),),
            clarifications=(Clarification("Usar bcrypt?", "Sim"),),
        )
        prompt = build_analyze_prompt(request, "python -m pytest")
        for text in ("systematic-debugging", "writing-plans", ANALYSIS_PATH, FIX_PLAN_PATH,
                     QUESTION_PATH, "FAILED test_login", "faltava hash", "FAILED test_hash",
                     "Usar bcrypt?", "Sim", "python -m pytest", "docs/superpowers/plans/ENG-12.md"):
            self.assertIn(text, prompt)
        self.assertIn("apagar, pular ou afrouxar testes", prompt)

    def test_analyze_prompt_omits_empty_sections(self) -> None:
        prompt = build_analyze_prompt(AnalyzeRequest("sbx-1", ISSUE, "FAILED"), "make test")
        self.assertNotIn("Tentativas anteriores", prompt)
        self.assertNotIn("Decisões da pessoa", prompt)

    def test_fix_prompt(self) -> None:
        prompt = build_fix_prompt(
            FixRequest("sbx-1", ISSUE, "análise", "plano",
                       (Clarification("Usar bcrypt?", "Sim"),)), "python -m pytest")
        for text in (FIX_PLAN_PATH, ANALYSIS_PATH, "python -m pytest", "git push",
                     "Usar bcrypt?", "apagar, pular ou afrouxar testes"):
            self.assertIn(text, prompt)


class TestPlanPath(unittest.TestCase):
    def test_plan_path(self) -> None:
        self.assertEqual(plan_path("ENG-12"), "docs/superpowers/plans/ENG-12.md")

    def test_plan_path_rejects_unsafe_identifier(self) -> None:
        for bad in ["ENG-12; rm -rf /", "../ENG-12", "ENG", ""]:
            with self.subTest(bad=bad), self.assertRaises(ApplicationError) as ctx:
                plan_path(bad)
            self.assertTrue(ctx.exception.non_retryable)


class TestSandboxActivities(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeSandbox.reset()
        self.activities = SandboxActivities(SETTINGS, sandbox_cls=FakeSandbox)
        self.env = ActivityEnvironment()
        self.heartbeats: list[Any] = []
        self.env.on_heartbeat = lambda *details: self.heartbeats.append(details)

    async def test_create_sandbox(self) -> None:
        sandbox_id = await self.env.run(self.activities.create_sandbox, "issue-1")
        self.assertEqual(sandbox_id, "sbx-1")
        (call,) = FakeSandbox.create_calls
        self.assertEqual(call["template"], "claude")
        self.assertEqual(call["envs"], {"ANTHROPIC_API_KEY": "anthropic-key"})
        self.assertEqual(call["metadata"], {"issue_id": "issue-1"})
        info = self.env.info
        self.assertEqual(
            call["idempotency_key"],
            f"{info.workflow_id}-{info.workflow_run_id}-{info.activity_id}".lower(),
        )
        self.assertEqual(call["api_key"], "e2b-key")

    async def test_create_sandbox_idempotency_key_accepted_by_e2b(self) -> None:
        # IDs reais do Temporal: workflow id do webhook, run id UUID e activity id numérico.
        info = replace(
            self.env.info,
            workflow_id="implement-issue-9b1D2c3e-AAAA",
            workflow_run_id="0193A1B2-C3D4-7E5F-8091-A2B3C4D5E6F7",
            activity_id="12",
        )
        env = ActivityEnvironment()
        env.info = info
        await env.run(self.activities.create_sandbox, "issue-1")
        (call,) = FakeSandbox.create_calls
        self.assertEqual(resolve_idempotency_key(call["idempotency_key"]), call["idempotency_key"])

    async def test_create_sandbox_with_oauth_token(self) -> None:
        settings = replace(SETTINGS, anthropic_api_key=None, claude_code_oauth_token="oat-token")
        activities = SandboxActivities(settings, sandbox_cls=FakeSandbox)
        await self.env.run(activities.create_sandbox, "issue-1")
        (call,) = FakeSandbox.create_calls
        self.assertEqual(call["envs"], {"CLAUDE_CODE_OAUTH_TOKEN": "oat-token"})

    async def test_clone_repo(self) -> None:
        await self.env.run(self.activities.clone_repo, "sbx-1")
        (run,) = FakeSandbox.instance.commands.runs
        self.assertIn("git clone --depth 1", run["cmd"])
        self.assertIn("github.com/acme/app.git", run["cmd"])
        self.assertNotIn("gh-token", run["cmd"])
        self.assertEqual(run["envs"], {"GITHUB_TOKEN": "gh-token"})

    async def test_clone_repo_skips_existing(self) -> None:
        FakeSandbox.instance.files.existing.add(f"{REPO_DIR}/.git")
        await self.env.run(self.activities.clone_repo, "sbx-1")
        self.assertEqual(FakeSandbox.instance.commands.runs, [])

    def claude_saves_plan(self, content: str = "# Plano\n1. fazer") -> None:
        files = FakeSandbox.instance.files

        def on_run(cmd: str) -> None:
            if "claude -p" in cmd:
                files.written[PLAN_FILE] = content

        FakeSandbox.instance.commands.on_run = on_run
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {}},
            {"type": "result", "subtype": "success", "result": "Plano salvo."},
        )

    async def test_generate_plan(self) -> None:
        self.claude_saves_plan()
        plan = await self.env.run(
            self.activities.generate_plan, PlanRequest(sandbox_id="sbx-1", issue=ISSUE)
        )
        self.assertEqual(plan, "# Plano\n1. fazer")
        files = FakeSandbox.instance.files
        self.assertIn("ENG-12", files.written["/home/user/prompt.md"])
        self.assertTrue(files.written[SKILL_DEST].startswith("---\nname: writing-plans"))
        (run,) = FakeSandbox.instance.commands.runs
        self.assertIn("claude -p", run["cmd"])
        self.assertIn("--output-format stream-json", run["cmd"])
        self.assertIn("Skill", run["cmd"])
        self.assertIn("Edit(docs/superpowers/plans/ENG-12.md)", run["cmd"])
        self.assertNotIn("Bash", run["cmd"])
        self.assertEqual(run["cwd"], REPO_DIR)
        self.assertNotIn("anthropic-key", run["cmd"])
        self.assertTrue(self.heartbeats)

    async def test_generate_plan_removes_stale_plan(self) -> None:
        FakeSandbox.instance.files.written[PLAN_FILE] = "antigo"
        self.claude_saves_plan("novo")
        plan = await self.env.run(
            self.activities.generate_plan, PlanRequest(sandbox_id="sbx-1", issue=ISSUE)
        )
        self.assertEqual(plan, "novo")
        self.assertIn(PLAN_FILE, FakeSandbox.instance.files.removed)

    async def test_generate_plan_missing_file(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "success", "result": "Plano salvo."}
        )
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.generate_plan, PlanRequest(sandbox_id="sbx-1", issue=ISSUE)
            )

    async def test_generate_plan_error_result(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "error_max_turns", "is_error": True}
        )
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.generate_plan, PlanRequest(sandbox_id="sbx-1", issue=ISSUE)
            )

    async def test_generate_plan_without_result(self) -> None:
        FakeSandbox.instance.commands.stdout = "não é json\n"
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.generate_plan, PlanRequest(sandbox_id="sbx-1", issue=ISSUE)
            )

    async def run_commit_plan(self, remote: str) -> str:
        FakeSandbox.instance.files.existing.add(PLAN_FILE)
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n", "ls-remote": remote}
        return await self.env.run(
            self.activities.commit_plan, CommitPlanRequest(sandbox_id="sbx-1", identifier="ENG-12")
        )

    def commands(self) -> list[str]:
        return [run["cmd"] for run in FakeSandbox.instance.commands.runs]

    async def test_commit_plan_pushes_new_branch(self) -> None:
        branch = await self.run_commit_plan(remote="")
        self.assertEqual(branch, "factory/ENG-12")
        self.assertEqual(
            self.commands(),
            [
                "git checkout -B factory/ENG-12",
                "git add docs/superpowers/plans/ENG-12.md",
                'git diff --cached --quiet || git -c user.name="software-factory"'
                ' -c user.email="software-factory@users.noreply.github.com"'
                ' commit -m "docs: plano de implementação ENG-12"',
                "git rev-parse HEAD",
                "git ls-remote --heads origin refs/heads/factory/ENG-12",
                "git push -u origin factory/ENG-12",
            ],
        )
        for run in FakeSandbox.instance.commands.runs:
            self.assertEqual(run["cwd"], REPO_DIR)
            self.assertNotIn("gh-token", run["cmd"])

    async def test_commit_plan_idempotent_after_push(self) -> None:
        branch = await self.run_commit_plan(remote="abc123\trefs/heads/factory/ENG-12\n")
        self.assertEqual(branch, "factory/ENG-12")
        self.assertFalse(any("git push" in cmd for cmd in self.commands()))

    async def test_commit_plan_rejects_diverged_remote(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_plan(remote="zzz999\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("factory/ENG-12", str(ctx.exception))
        self.assertFalse(any("git push" in cmd for cmd in self.commands()))

    async def test_commit_plan_missing_plan_file(self) -> None:
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.commit_plan,
                CommitPlanRequest(sandbox_id="sbx-1", identifier="ENG-12"),
            )
        self.assertEqual(self.commands(), [])

    async def test_clone_branch(self) -> None:
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n"}
        sha = await self.env.run(
            self.activities.clone_branch, CloneBranchRequest(sandbox_id="sbx-1", identifier="ENG-12")
        )
        self.assertEqual(sha, "abc123")
        runs = FakeSandbox.instance.commands.runs
        self.assertIn("git clone --depth 1 --branch factory/ENG-12", runs[0]["cmd"])
        self.assertIn("$GITHUB_TOKEN@github.com/acme/app.git", runs[0]["cmd"])
        self.assertEqual(runs[0]["envs"], {"GITHUB_TOKEN": "gh-token"})
        self.assertEqual(
            self.commands()[1:],
            [
                "git remote set-url origin https://github.com/acme/app.git",
                'git config user.name "software-factory"',
                'git config user.email "software-factory@users.noreply.github.com"',
                "git rev-parse refs/remotes/origin/factory/ENG-12",
            ],
        )
        for run in runs:
            self.assertNotIn("gh-token", run["cmd"])
        for run in runs[1:]:
            self.assertEqual(run["cwd"], REPO_DIR)

    async def test_clone_branch_retry_skips_clone_but_resets_origin(self) -> None:
        FakeSandbox.instance.files.existing.add(f"{REPO_DIR}/.git")
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n"}
        await self.env.run(
            self.activities.clone_branch, CloneBranchRequest(sandbox_id="sbx-1", identifier="ENG-12")
        )
        commands = self.commands()
        self.assertFalse(any("git clone" in cmd for cmd in commands))
        self.assertIn("git remote set-url origin https://github.com/acme/app.git", commands)
        self.assertEqual(commands[-1], "git rev-parse refs/remotes/origin/factory/ENG-12")

    async def test_implement_plan(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "assistant", "message": {}},
            {"type": "result", "subtype": "success", "result": "Implementado."},
        )
        result = await self.env.run(
            self.activities.implement_plan, ImplementRequest(sandbox_id="sbx-1", issue=ISSUE)
        )
        self.assertEqual(result, "Implementado.")
        self.assertIn("ENG-12", FakeSandbox.instance.files.written["/home/user/implement-prompt.md"])
        (run,) = FakeSandbox.instance.commands.runs
        self.assertIn("cat /home/user/implement-prompt.md | claude -p", run["cmd"])
        self.assertIn("--output-format stream-json", run["cmd"])
        self.assertIn("Bash", run["cmd"])
        self.assertEqual(run["cwd"], REPO_DIR)
        self.assertTrue(self.heartbeats)

    async def test_implement_plan_error_result(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "error_max_turns", "is_error": True}
        )
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.implement_plan, ImplementRequest(sandbox_id="sbx-1", issue=ISSUE)
            )

    async def test_run_tests_green(self) -> None:
        FakeSandbox.instance.commands.stdout = "5 passed"
        result = await self.env.run(self.activities.run_tests, "sbx-1")
        self.assertEqual(result, TestRunResult(True, "5 passed"))
        (run,) = FakeSandbox.instance.commands.runs
        self.assertEqual(run["cmd"], "python -m pytest")
        self.assertEqual(run["cwd"], REPO_DIR)

    async def test_run_tests_red(self) -> None:
        FakeSandbox.instance.commands.fail = {"pytest": ("x" * 5000, "FAILED test_a", 1)}
        result = await self.env.run(self.activities.run_tests, "sbx-1")
        self.assertFalse(result.passed)
        self.assertEqual(len(result.output), 4000)
        self.assertTrue(result.output.endswith("FAILED test_a"))

    async def run_commit_implementation(self, head: str, remote: str, **kwargs: Any) -> str:
        FakeSandbox.instance.commands.responses = {"rev-parse": head, "ls-remote": remote}
        return await self.env.run(
            self.activities.commit_implementation,
            CommitImplementationRequest(
                sandbox_id="sbx-1", identifier="ENG-12", base_sha="abc123", **kwargs
            ),
        )

    async def test_commit_implementation_checkpoint_message(self) -> None:
        await self.run_commit_implementation(
            "def456\n",
            "abc123\trefs/heads/factory/ENG-12\n",
            message="wip: tentativa 1 de ENG-12 (testes vermelhos)",
            require_changes=False,
        )
        runs = FakeSandbox.instance.commands.runs
        commit = next(r["cmd"] for r in runs if "git commit" in r["cmd"])
        self.assertIn('-m "wip: tentativa 1 de ENG-12 (testes vermelhos)"', commit)
        self.assertTrue(any("git push" in r["cmd"] for r in runs))

    async def test_commit_implementation_no_changes_allowed(self) -> None:
        result = await self.run_commit_implementation(
            "abc123\n", "abc123\trefs/heads/factory/ENG-12\n", require_changes=False
        )
        self.assertEqual(result, "factory/ENG-12")
        runs = FakeSandbox.instance.commands.runs
        self.assertFalse(any("git push" in r["cmd"] for r in runs))
        self.assertFalse(any("ls-remote" in r["cmd"] for r in runs))

    async def test_commit_implementation_pushes(self) -> None:
        branch = await self.run_commit_implementation(
            "def456\n", "abc123\trefs/heads/factory/ENG-12\n"
        )
        self.assertEqual(branch, "factory/ENG-12")
        runs = FakeSandbox.instance.commands.runs
        self.assertEqual(runs[0]["cmd"], "git add -A")
        self.assertIn('git commit -m "feat: implementa ENG-12"', runs[1]["cmd"])
        self.assertEqual(
            runs[-1]["cmd"],
            'git push "https://x-access-token:$GITHUB_TOKEN@github.com/acme/app.git"'
            " HEAD:refs/heads/factory/ENG-12",
        )
        self.assertEqual(runs[-1]["envs"], {"GITHUB_TOKEN": "gh-token"})
        ls_remote = next(run for run in runs if "ls-remote" in run["cmd"])
        self.assertEqual(ls_remote["envs"], {"GITHUB_TOKEN": "gh-token"})
        for run in runs:
            self.assertNotIn("gh-token", run["cmd"])
            self.assertEqual(run["cwd"], REPO_DIR)

    async def test_commit_implementation_idempotent_after_push(self) -> None:
        branch = await self.run_commit_implementation(
            "def456\n", "def456\trefs/heads/factory/ENG-12\n"
        )
        self.assertEqual(branch, "factory/ENG-12")
        self.assertFalse(any("git push" in cmd for cmd in self.commands()))

    async def test_commit_implementation_rejects_no_changes(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_implementation("abc123\n", "abc123\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertFalse(any("ls-remote" in cmd or "git push" in cmd for cmd in self.commands()))

    async def test_commit_implementation_rejects_moved_remote(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_implementation("def456\n", "zzz999\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertFalse(any("git push" in cmd for cmd in self.commands()))

    def claude_writes(self, files_to_write: dict[str, str]) -> None:
        files = FakeSandbox.instance.files

        def on_run(cmd: str) -> None:
            if "claude -p" in cmd:
                files.written.update(files_to_write)

        FakeSandbox.instance.commands.on_run = on_run
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "success", "result": "<texto do result>"}
        )

    async def test_analyze_failure_returns_plan(self) -> None:
        self.claude_writes({ANALYSIS_PATH: "causa", FIX_PLAN_PATH: "plano"})
        result = await self.env.run(
            self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED")
        )
        self.assertEqual(result, FixAnalysis(analysis="causa", plan="plano"))
        files = FakeSandbox.instance.files.written
        self.assertIn(f"{SKILLS_DEST_DIR}/systematic-debugging/SKILL.md", files)
        self.assertIn(f"{SKILLS_DEST_DIR}/writing-plans/SKILL.md", files)
        runs = FakeSandbox.instance.commands.runs
        self.assertTrue(any(f"rm -rf {FIX_DIR}" in r["cmd"] for r in runs))
        self.assertTrue(self.heartbeats)

    async def test_analyze_failure_returns_question(self) -> None:
        self.claude_writes({ANALYSIS_PATH: "causa", QUESTION_PATH: "Trocar o ORM?"})
        result = await self.env.run(
            self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED")
        )
        self.assertEqual(result, FixAnalysis(analysis="causa", question="Trocar o ORM?"))

    async def test_analyze_failure_without_plan_or_question(self) -> None:
        self.claude_writes({ANALYSIS_PATH: "causa"})
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED")
            )

    async def test_implement_fix_writes_plan_and_runs_claude(self) -> None:
        self.claude_writes({})
        result = await self.env.run(
            self.activities.implement_fix, FixRequest("sbx-1", ISSUE, "causa", "plano")
        )
        files = FakeSandbox.instance.files.written
        self.assertEqual(files[FIX_PLAN_PATH], "plano")
        self.assertEqual(files[ANALYSIS_PATH], "causa")
        self.assertIn(FIX_PROMPT_PATH, files)
        self.assertEqual(result, "<texto do result>")

    async def test_kill_sandbox(self) -> None:
        await self.env.run(self.activities.kill_sandbox, "sbx-1")
        self.assertEqual(FakeSandbox.killed, ["sbx-1"])


if __name__ == "__main__":
    unittest.main()
