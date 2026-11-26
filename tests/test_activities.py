import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable
from unittest import mock

from e2b import CommandExitException, FileNotFoundException, TimeoutException
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
from software_factory.orchestration.token_store import TokenStore
from software_factory.orchestration.models import (
    AnalyzeRequest,
    CloneBranchRequest,
    CloneRepoRequest,
    Clarification,
    CommitImplementationRequest,
    CommitPlanRequest,
    FixAnalysis,
    FixAttempt,
    FixRequest,
    ImplementRequest,
    PlanIssueInput,
    PlanRequest,
    RepoTarget,
    RunTestsRequest,
    TestRunResult,
    UsageAlert,
)
from software_factory.orchestration.token_usage import TokenCounts
from tests.stream_samples import INIT_EVENT, ASSISTANT_EVENT, RESULT_EVENT

SETTINGS = FactorySettings(
    e2b_api_key="e2b-key",
    anthropic_api_key="anthropic-key",
    github_app_id="123",
    github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    linear_api_key="lin-key",
)

PEM = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n"
KEY_PATH = ""
_key_dir = tempfile.TemporaryDirectory()


def setUpModule() -> None:
    global KEY_PATH
    KEY_PATH = os.path.join(_key_dir.name, "app.pem")
    with open(KEY_PATH, "w") as f:
        f.write(PEM)


def tearDownModule() -> None:
    _key_dir.cleanup()


async def tokens(repo: str) -> str:
    return "gh-token"


TARGET = RepoTarget("acme/app", "python -m pytest")

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
    # Awaited antes de responder: deixa o teste observar comandos concorrentes.
    hook: Callable[[str], Awaitable[None]] | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, cmd: str, **kwargs: Any) -> FakeResult:
        self.runs.append({"cmd": cmd, **kwargs})
        if self.hook:
            await self.hook(cmd)
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


USAGE_RESET = 1795440000
USAGE_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": USAGE_RESET}},
    },
}


RESULT_EVENT_OK = {**RESULT_EVENT, "subtype": "success", "result": "Ok.", "is_error": False}


class FakeNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.alerts: list[UsageAlert] = []

    async def notify(self, alert: UsageAlert) -> bool:
        self.alerts.append(alert)
        if self.error:
            raise self.error
        return True


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
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                FactorySettings.from_env()
        with mock.patch.dict(os.environ, {**env, "LINEAR_API_KEY": "lin-key"}, clear=True):
            self.assertEqual(FactorySettings.from_env().linear_api_key, "lin-key")

    def test_from_env_reads_app_private_key_file(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = FactorySettings.from_env()
        self.assertEqual(settings.github_app_id, "123")
        self.assertTrue(settings.github_app_private_key.startswith("-----BEGIN"))
        self.assertNotIn(settings.github_app_private_key, repr(settings))
        self.assertNotIn("BEGIN", repr(settings))

    def test_from_env_requires_app_id(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_APP_ID não definida"):
                FactorySettings.from_env()

    def test_from_env_missing_key_file(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": os.path.join(_key_dir.name, "nao-existe.pem"),
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_APP_PRIVATE_KEY_PATH"):
                FactorySettings.from_env()

    def test_from_env_no_longer_needs_github_token(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(hasattr(FactorySettings.from_env(), "github_token"))

    def test_from_env_without_target_repo_and_test_command(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = FactorySettings.from_env()
        self.assertFalse(hasattr(settings, "target_repo"))
        self.assertFalse(hasattr(settings, "test_command"))

    def test_token_usage_db_default_and_env(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(FactorySettings.from_env().token_usage_db, "token_usage.db")
        with mock.patch.dict(os.environ, {**env, "TOKEN_USAGE_DB": "/data/t.db"}, clear=True):
            self.assertEqual(FactorySettings.from_env().token_usage_db, "/data/t.db")

    def test_alert_email_optional(self) -> None:
        env = {
            "E2B_API_KEY": "e2b-key",
            "ANTHROPIC_API_KEY": "anthropic-key",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_PRIVATE_KEY_PATH": KEY_PATH,
            "LINEAR_API_KEY": "lin-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(FactorySettings.from_env().alert_email)
        smtp = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "bot@example.com",
            "SMTP_PASSWORD": "pw",
            "ALERT_EMAIL_TO": "dev@example.com",
        }
        with mock.patch.dict(os.environ, {**env, **smtp}, clear=True):
            alert_email = FactorySettings.from_env().alert_email
            assert alert_email is not None
            self.assertEqual(alert_email.host, "smtp.example.com")


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
            "sbx-1", ISSUE, test_output="FAILED test_login", target=TARGET,
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
        prompt = build_analyze_prompt(AnalyzeRequest("sbx-1", ISSUE, "FAILED", TARGET), "make test")
        self.assertNotIn("Tentativas anteriores", prompt)
        self.assertNotIn("Decisões da pessoa", prompt)

    def test_fix_prompt(self) -> None:
        prompt = build_fix_prompt(
            FixRequest("sbx-1", ISSUE, "análise", "plano", TARGET,
                       (Clarification("Usar bcrypt?", "Sim"),)), "python -m pytest")
        for text in (FIX_PLAN_PATH, ANALYSIS_PATH, "python -m pytest", "git push",
                     "Usar bcrypt?", "apagar, pular ou afrouxar testes"):
            self.assertIn(text, prompt)


    def test_fix_prompt_forbids_changing_factory_toml(self) -> None:
        prompt = build_fix_prompt(
            FixRequest("sbx-1", ISSUE, "análise", "plano", TARGET), "python -m pytest"
        )
        self.assertIn("`.factory.toml`", prompt)
        self.assertNotIn("TEST_COMMAND", prompt)


class TestPlanPath(unittest.TestCase):
    def test_plan_path(self) -> None:
        self.assertEqual(plan_path("ENG-12"), "docs/superpowers/plans/ENG-12.md")

    def test_plan_path_rejects_unsafe_identifier(self) -> None:
        for bad in ["ENG-12; rm -rf /", "../ENG-12", "ENG", ""]:
            with self.subTest(bad=bad), self.assertRaises(ApplicationError) as ctx:
                plan_path(bad)
            self.assertTrue(ctx.exception.non_retryable)


AUTHED_URL = '"https://x-access-token:$GITHUB_TOKEN@github.com/acme/app.git"'


class TestSandboxActivities(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeSandbox.reset()
        self.activities = SandboxActivities(SETTINGS, tokens, sandbox_cls=FakeSandbox)
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
        activities = SandboxActivities(settings, tokens, sandbox_cls=FakeSandbox)
        await self.env.run(activities.create_sandbox, "issue-1")
        (call,) = FakeSandbox.create_calls
        self.assertEqual(call["envs"], {"CLAUDE_CODE_OAUTH_TOKEN": "oat-token"})

    async def test_clone_repo(self) -> None:
        await self.env.run(self.activities.clone_repo, CloneRepoRequest("sbx-1", TARGET))
        run = FakeSandbox.instance.commands.runs[0]
        self.assertIn("git clone --depth 1", run["cmd"])
        self.assertIn("github.com/acme/app.git", run["cmd"])
        self.assertNotIn("gh-token", run["cmd"])
        self.assertEqual(run["envs"], {"GITHUB_TOKEN": "gh-token"})

    async def test_clone_repo_leaves_no_token_in_origin(self) -> None:
        await self.env.run(self.activities.clone_repo, CloneRepoRequest("sbx-1", TARGET))
        runs = FakeSandbox.instance.commands.runs
        self.assertEqual(runs[0]["envs"], {"GITHUB_TOKEN": "gh-token"})
        self.assertIn(
            "git remote set-url origin https://github.com/acme/app.git",
            [r["cmd"] for r in runs],
        )
        self.assertFalse(any("gh-token" in r["cmd"] for r in runs))

    async def test_clone_repo_skips_existing(self) -> None:
        FakeSandbox.instance.files.existing.add(f"{REPO_DIR}/.git")
        await self.env.run(self.activities.clone_repo, CloneRepoRequest("sbx-1", TARGET))
        cmds = [r["cmd"] for r in FakeSandbox.instance.commands.runs]
        self.assertEqual(cmds, ["git remote set-url origin https://github.com/acme/app.git"])

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
        # Absoluta (//): regra relativa segue o cwd do Bash e nega o Write depois de um `cd`.
        self.assertIn("Edit(//home/user/repo/docs/superpowers/plans/ENG-12.md)", run["cmd"])
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
            self.activities.commit_plan, CommitPlanRequest("sbx-1", "ENG-12", TARGET)
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
                "git diff --cached --quiet || git -c core.hooksPath=/dev/null"
                ' -c user.name="software-factory"'
                ' -c user.email="software-factory@users.noreply.github.com"'
                ' commit -m "docs: plano de implementação ENG-12"',
                "git rev-parse HEAD",
                f"git ls-remote --heads {AUTHED_URL} refs/heads/factory/ENG-12",
                f"git -c core.hooksPath=/dev/null push {AUTHED_URL} HEAD:refs/heads/factory/ENG-12",
            ],
        )
        for run in FakeSandbox.instance.commands.runs:
            self.assertEqual(run["cwd"], REPO_DIR)
            self.assertNotIn("gh-token", run["cmd"])

    async def test_commit_plan_pushes_with_fresh_token(self) -> None:
        calls = 0

        async def tokens(repo: str) -> str:
            nonlocal calls
            calls += 1
            return f"tok-{calls}"

        activities = SandboxActivities(SETTINGS, tokens, sandbox_cls=FakeSandbox)
        await self.env.run(activities.clone_repo, CloneRepoRequest("sbx-1", TARGET))
        FakeSandbox.instance.files.existing.add(PLAN_FILE)
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n", "ls-remote": ""}
        await self.env.run(
            activities.commit_plan, CommitPlanRequest("sbx-1", "ENG-12", TARGET)
        )
        runs = FakeSandbox.instance.commands.runs
        push = next(r for r in runs if " push " in r["cmd"])
        self.assertIn("x-access-token:$GITHUB_TOKEN@github.com/acme/app.git", push["cmd"])
        self.assertEqual(push["envs"], {"GITHUB_TOKEN": "tok-2"})
        ls_remote = next(r for r in runs if "ls-remote" in r["cmd"])
        self.assertIn("x-access-token:$GITHUB_TOKEN@github.com/acme/app.git", ls_remote["cmd"])
        self.assertEqual(ls_remote["envs"], {"GITHUB_TOKEN": "tok-2"})
        self.assertEqual(calls, 2)  # clone_repo + commit_plan (uma vez)

    async def test_commit_plan_idempotent_after_push(self) -> None:
        branch = await self.run_commit_plan(remote="abc123\trefs/heads/factory/ENG-12\n")
        self.assertEqual(branch, "factory/ENG-12")
        self.assertFalse(any(" push " in cmd for cmd in self.commands()))

    async def test_commit_plan_rejects_diverged_remote(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_plan(remote="zzz999\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertIn("factory/ENG-12", str(ctx.exception))
        self.assertFalse(any(" push " in cmd for cmd in self.commands()))

    async def test_commit_plan_missing_plan_file(self) -> None:
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.commit_plan,
                CommitPlanRequest(sandbox_id="sbx-1", identifier="ENG-12", target=TARGET),
            )
        self.assertEqual(self.commands(), [])

    async def test_clone_branch(self) -> None:
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n"}
        sha = await self.env.run(
            self.activities.clone_branch, CloneBranchRequest("sbx-1", "ENG-12", TARGET)
        )
        self.assertEqual(sha, "abc123")
        runs = FakeSandbox.instance.commands.runs
        self.assertIn("git clone --depth 50 --branch factory/ENG-12", runs[0]["cmd"])
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

    async def test_clone_branch_uses_target_repo(self) -> None:
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n"}
        await self.env.run(
            self.activities.clone_branch,
            CloneBranchRequest("sbx-1", "ENG-12", RepoTarget("acme/api", "npm test")),
        )
        runs = FakeSandbox.instance.commands.runs
        self.assertIn("x-access-token:$GITHUB_TOKEN@github.com/acme/api.git", runs[0]["cmd"])
        self.assertIn("git remote set-url origin https://github.com/acme/api.git", self.commands())
        self.assertFalse(any("acme/app" in cmd for cmd in self.commands()))

    async def test_clone_branch_retry_skips_clone_but_resets_origin(self) -> None:
        FakeSandbox.instance.files.existing.add(f"{REPO_DIR}/.git")
        FakeSandbox.instance.commands.responses = {"rev-parse": "abc123\n"}
        await self.env.run(
            self.activities.clone_branch, CloneBranchRequest("sbx-1", "ENG-12", TARGET)
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
            self.activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
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
                self.activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
            )

    def with_notifier(self, notifier: FakeNotifier) -> SandboxActivities:
        return SandboxActivities(SETTINGS, tokens, sandbox_cls=FakeSandbox, usage_notifier=notifier)

    async def test_implement_plan_notifies_usage(self) -> None:
        notifier = FakeNotifier()
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Implementado."}
        )
        result = await self.env.run(
            self.with_notifier(notifier).implement_plan,
            ImplementRequest("sbx-1", ISSUE, TARGET),
        )
        self.assertEqual(result, "Implementado.")
        source = f"{self.env.info.workflow_id}/{self.env.info.activity_type}"
        self.assertEqual(notifier.alerts, [UsageAlert("five_hour", 0.85, USAGE_RESET, source)])

    async def test_usage_notifier_failure_does_not_fail_session(self) -> None:
        notifier = FakeNotifier(RuntimeError("temporal fora"))
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Implementado."}
        )
        result = await self.env.run(
            self.with_notifier(notifier).implement_plan,
            ImplementRequest("sbx-1", ISSUE, TARGET),
        )
        self.assertEqual(result, "Implementado.")
        self.assertEqual(len(notifier.alerts), 1)

    async def test_usage_watcher_error_does_not_fail_session(self) -> None:
        notifier = FakeNotifier()
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Ok."}
        )
        with mock.patch(
            "software_factory.orchestration.activities.UsageWatcher.feed",
            side_effect=RecursionError("json aninhado"),
        ):
            result = await self.env.run(
                self.with_notifier(notifier).implement_plan,
                ImplementRequest("sbx-1", ISSUE, TARGET),
            )
        self.assertEqual(result, "Ok.")

    def with_store(
        self, store: TokenStore, workflow_id: str = "implement-issue-abc", **kwargs: Any
    ) -> tuple[SandboxActivities, ActivityEnvironment]:
        env = ActivityEnvironment()
        env.info = replace(env.info, workflow_id=workflow_id)
        activities = SandboxActivities(
            SETTINGS, tokens, sandbox_cls=FakeSandbox, token_store=store, **kwargs
        )
        return activities, env

    def new_store(self) -> TokenStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return TokenStore(os.path.join(tmp.name, "tokens.db"))

    async def test_records_session_usage(self) -> None:
        store = self.new_store()
        FakeSandbox.instance.commands.stdout = stream(
            INIT_EVENT, ASSISTANT_EVENT, RESULT_EVENT_OK
        )
        activities, env = self.with_store(store)
        await env.run(
            activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
        )
        usage = store.issue_usage("abc")
        self.assertEqual(usage.stages["implement"].tokens, TokenCounts(4, 175, 40261, 37118))
        self.assertFalse(usage.stages["implement"].partial)

    async def test_records_usage_when_session_times_out(self) -> None:
        store = self.new_store()
        FakeSandbox.instance.commands.stdout = stream(INIT_EVENT, ASSISTANT_EVENT)

        def time_out(cmd: str) -> None:
            raise TimeoutException("context deadline exceeded")

        FakeSandbox.instance.commands.on_run = time_out
        activities, env = self.with_store(store)
        with self.assertRaises(TimeoutException):
            await env.run(
                activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
            )
        self.assertTrue(store.issue_usage("abc").stages["implement"].partial)

    async def test_store_error_does_not_fail_session(self) -> None:
        store = self.new_store()
        FakeSandbox.instance.commands.stdout = stream(
            INIT_EVENT, ASSISTANT_EVENT, RESULT_EVENT_OK
        )
        activities, env = self.with_store(store)
        with mock.patch.object(store, "record", side_effect=sqlite3.OperationalError("locked")):
            result = await env.run(
                activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
            )
        self.assertEqual(result, "Ok.")

    async def test_no_record_outside_issue_workflow(self) -> None:
        store = self.new_store()
        FakeSandbox.instance.commands.stdout = stream(
            INIT_EVENT, ASSISTANT_EVENT, RESULT_EVENT_OK
        )
        activities, env = self.with_store(store, workflow_id="outro-x")
        with mock.patch.object(store, "record") as record:
            result = await env.run(
                activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
            )
        self.assertEqual(result, "Ok.")
        record.assert_not_called()

    async def test_usage_alert_still_fires(self) -> None:
        store = self.new_store()
        notifier = FakeNotifier()
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Ok."}
        )
        activities, env = self.with_store(store, usage_notifier=notifier)
        await env.run(
            activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
        )
        self.assertEqual(len(notifier.alerts), 1)
        self.assertEqual(store.issue_usage("abc").stages["implement"].tokens, TokenCounts())

    async def test_run_tests_not_watched(self) -> None:
        notifier = FakeNotifier()
        FakeSandbox.instance.commands.stdout = stream(USAGE_EVENT)
        result = await self.env.run(
            self.with_notifier(notifier).run_tests, RunTestsRequest("sbx-1", TARGET)
        )
        self.assertTrue(result.passed)
        self.assertEqual(notifier.alerts, [])

    async def test_no_notifier(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            USAGE_EVENT, {"type": "result", "subtype": "success", "result": "Implementado."}
        )
        result = await self.env.run(
            self.activities.implement_plan, ImplementRequest("sbx-1", ISSUE, TARGET)
        )
        self.assertEqual(result, "Implementado.")

    async def test_run_tests_green(self) -> None:
        FakeSandbox.instance.commands.stdout = "5 passed"
        result = await self.env.run(self.activities.run_tests, RunTestsRequest("sbx-1", TARGET))
        self.assertEqual(result, TestRunResult(True, "5 passed"))
        (run,) = FakeSandbox.instance.commands.runs
        self.assertEqual(run["cmd"], "python -m pytest")
        self.assertEqual(run["cwd"], REPO_DIR)

    async def test_run_tests_uses_target_command(self) -> None:
        await self.env.run(
            self.activities.run_tests, RunTestsRequest("sbx-1", RepoTarget("acme/app", "npm test"))
        )
        runs = FakeSandbox.instance.commands.runs
        self.assertEqual(runs[-1]["cmd"], "npm test")

    async def test_implement_prompt_uses_target_command(self) -> None:
        FakeSandbox.instance.commands.stdout = stream(
            {"type": "result", "subtype": "success", "result": "Implementado."}
        )
        await self.env.run(
            self.activities.implement_plan,
            ImplementRequest("sbx-1", ISSUE, RepoTarget("acme/app", "npm test")),
        )
        prompt = FakeSandbox.instance.files.written["/home/user/implement-prompt.md"]
        self.assertIn("`npm test`", prompt)
        self.assertNotIn("python -m pytest", prompt)

    async def test_run_tests_red(self) -> None:
        FakeSandbox.instance.commands.fail = {"pytest": ("x" * 5000, "FAILED test_a", 1)}
        result = await self.env.run(self.activities.run_tests, RunTestsRequest("sbx-1", TARGET))
        self.assertFalse(result.passed)
        self.assertEqual(len(result.output), 4000)
        self.assertTrue(result.output.endswith("FAILED test_a"))

    async def test_run_tests_timeout_is_red(self) -> None:
        def time_out(cmd: str) -> None:
            raise TimeoutException("context deadline exceeded")

        FakeSandbox.instance.commands.on_run = time_out
        result = await self.env.run(self.activities.run_tests, RunTestsRequest("sbx-1", TARGET))
        self.assertFalse(result.passed)
        self.assertIn("o comando de testes estourou o timeout de 10 min", result.output)

    async def run_commit_implementation(self, head: str, remote: str, **kwargs: Any) -> str:
        FakeSandbox.instance.commands.responses = {"rev-parse": head, "ls-remote": remote}
        return await self.env.run(
            self.activities.commit_implementation,
            CommitImplementationRequest(
                sandbox_id="sbx-1", identifier="ENG-12", base_sha="abc123", target=TARGET,
                **kwargs,
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
        commit = next(r["cmd"] for r in runs if " commit " in r["cmd"])
        self.assertIn('-m "wip: tentativa 1 de ENG-12 (testes vermelhos)"', commit)
        self.assertTrue(any(" push " in r["cmd"] for r in runs))

    async def test_commit_implementation_no_changes_allowed(self) -> None:
        result = await self.run_commit_implementation(
            "abc123\n", "abc123\trefs/heads/factory/ENG-12\n", require_changes=False
        )
        self.assertEqual(result, "factory/ENG-12")
        runs = FakeSandbox.instance.commands.runs
        self.assertFalse(any(" push " in r["cmd"] for r in runs))
        self.assertFalse(any("ls-remote" in r["cmd"] for r in runs))

    async def test_commit_implementation_pushes(self) -> None:
        branch = await self.run_commit_implementation(
            "def456\n", "abc123\trefs/heads/factory/ENG-12\n"
        )
        self.assertEqual(branch, "factory/ENG-12")
        runs = FakeSandbox.instance.commands.runs
        self.assertEqual(runs[0]["cmd"], "git add -A")
        self.assertIn(
            'git -c core.hooksPath=/dev/null commit -m "feat: implementa ENG-12"', runs[1]["cmd"]
        )
        self.assertEqual(
            runs[-1]["cmd"],
            'git -c core.hooksPath=/dev/null push "https://x-access-token:$GITHUB_TOKEN@github.com/acme/app.git"'
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
        self.assertFalse(any(" push " in cmd for cmd in self.commands()))

    async def test_commit_implementation_rejects_no_changes(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_implementation("abc123\n", "abc123\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertFalse(any("ls-remote" in cmd or " push " in cmd for cmd in self.commands()))

    async def test_commit_implementation_rejects_moved_remote(self) -> None:
        with self.assertRaises(ApplicationError) as ctx:
            await self.run_commit_implementation("def456\n", "zzz999\trefs/heads/factory/ENG-12\n")
        self.assertTrue(ctx.exception.non_retryable)
        self.assertFalse(any(" push " in cmd for cmd in self.commands()))

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
            self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED", TARGET)
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
            self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED", TARGET)
        )
        self.assertEqual(result, FixAnalysis(analysis="causa", question="Trocar o ORM?"))

    async def test_analyze_failure_without_plan_or_question(self) -> None:
        self.claude_writes({ANALYSIS_PATH: "causa"})
        with self.assertRaises(ApplicationError):
            await self.env.run(
                self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED", TARGET)
            )

    async def test_implement_fix_writes_plan_and_runs_claude(self) -> None:
        self.claude_writes({})
        result = await self.env.run(
            self.activities.implement_fix, FixRequest("sbx-1", ISSUE, "causa", "plano", TARGET)
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
