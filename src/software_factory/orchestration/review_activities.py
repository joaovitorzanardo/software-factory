"""Sessão de review no sandbox: lentes correctness/security em paralelo, triagem e correção."""

import asyncio
import dataclasses
import re
from e2b import FileNotFoundException
from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.orchestration.activities import (
    GIT_COMMAND_TIMEOUT_S,
    IMPLEMENT_COMMAND_TIMEOUT_S,
    REPO_DIR,
    SandboxActivities,
    install_skills,
    parse_result,
)
from software_factory.orchestration.models import ReviewFixRequest, ReviewReport, ReviewRequest
from software_factory.orchestration.review_prompts import (
    CORRECTNESS_PATH,
    REVIEW_DIR,
    REVIEW_JSON_PATH,
    SECURITY_PATH,
    build_correctness_prompt,
    build_review_fix_prompt,
    build_security_prompt,
    build_triage_prompt,
    parse_review,
    sensitive_files,
)

CORRECTNESS_PROMPT_PATH = "/home/user/review-correctness-prompt.md"
SECURITY_PROMPT_PATH = "/home/user/review-security-prompt.md"
TRIAGE_PROMPT_PATH = "/home/user/review-triage-prompt.md"
REVIEW_FIX_PROMPT_PATH = "/home/user/review-fix-prompt.md"
# Cabe no sandbox de 55 min: clone + fetch + lentes em paralelo + triagem.
REVIEW_LENS_TIMEOUT_S = 12 * 60
REVIEW_TRIAGE_TIMEOUT_S = 10 * 60
REVIEW_TOOLS = "Read,Grep,Glob,Skill,Bash,Write"
REVIEW_FIX_TOOLS = "Read,Grep,Glob,Skill,Edit,Write,Bash"
# O SHA vai para comando shell e prompts: só hex.
SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _claude_command(prompt_path: str, tools: str) -> str:
    return (
        f"cat {prompt_path} | claude -p --output-format stream-json --verbose"
        f' --allowedTools "{tools}"'
    )


def review_lens_command(prompt_path: str) -> str:
    return _claude_command(prompt_path, REVIEW_TOOLS)


def review_triage_command() -> str:
    return _claude_command(TRIAGE_PROMPT_PATH, REVIEW_TOOLS)


def review_fix_command() -> str:
    return _claude_command(REVIEW_FIX_PROMPT_PATH, REVIEW_FIX_TOOLS)


class ReviewActivities:
    def __init__(self, sandbox: SandboxActivities) -> None:
        self.sandbox = sandbox

    @activity.defn
    async def review_pull(self, request: ReviewRequest) -> ReviewReport:
        """Roda as duas lentes em paralelo e a triagem; devolve o review.json validado."""
        if not SHA_RE.fullmatch(request.base_sha):
            raise ApplicationError(f"base_sha inválido: {request.base_sha!r}", non_retryable=True)
        sandbox = await self.sandbox._connect(request.sandbox_id)
        await install_skills(sandbox)
        # Retry e rodadas novas não herdam relatórios velhos.
        await sandbox.commands.run(
            f"rm -rf {REVIEW_DIR} && mkdir -p {REVIEW_DIR}", timeout=GIT_COMMAND_TIMEOUT_S
        )
        # Clone raso: o merge-base precisa estar local para o `git diff <base>..HEAD`.
        repo = self.sandbox.settings.target_repo
        await sandbox.commands.run(
            f"git fetch --depth 1 {self.sandbox._authed_url(repo)} {request.base_sha}",
            cwd=REPO_DIR,
            envs=await self.sandbox._token_env(repo),
            timeout=GIT_COMMAND_TIMEOUT_S,
        )
        # Portão do orquestrador, antes de qualquer Claude: o código revisado não o controla.
        diff = await sandbox.commands.run(
            f"git diff --name-only {request.base_sha} HEAD",
            cwd=REPO_DIR,
            timeout=GIT_COMMAND_TIMEOUT_S,
        )
        sensitive = sensitive_files([p for p in diff.stdout.splitlines() if p.strip()])
        await sandbox.files.write(CORRECTNESS_PROMPT_PATH, build_correctness_prompt(request))
        await sandbox.files.write(SECURITY_PROMPT_PATH, build_security_prompt(request))
        await sandbox.files.write(TRIAGE_PROMPT_PATH, build_triage_prompt(request))

        lenses = [
            asyncio.create_task(
                self.sandbox._run_claude(sandbox, review_lens_command(path), REVIEW_LENS_TIMEOUT_S)
            )
            for path in (CORRECTNESS_PROMPT_PATH, SECURITY_PROMPT_PATH)
        ]
        try:
            results = await asyncio.gather(*lenses)
        finally:
            # Uma lente que falha não deixa a outra rodando solta.
            for task in lenses:
                task.cancel()
        for result in results:
            parse_result(result.stdout)
        for path in (CORRECTNESS_PATH, SECURITY_PATH):
            if not await sandbox.files.exists(path):
                raise ApplicationError(f"Claude Code não salvou o relatório em {path}")

        result = await self.sandbox._run_claude(
            sandbox, review_triage_command(), REVIEW_TRIAGE_TIMEOUT_S
        )
        parse_result(result.stdout)
        try:
            text = await sandbox.files.read(REVIEW_JSON_PATH)
        except FileNotFoundException:
            raise ApplicationError(
                f"Claude Code não salvou a triagem em {REVIEW_JSON_PATH}"
            ) from None
        return dataclasses.replace(parse_review(text), sensitive_files=sensitive)

    @activity.defn
    async def implement_review_fix(self, request: ReviewFixRequest) -> str:
        """Corrige os achados `fix` da rodada; devolve o resumo final (as fix_notes)."""
        sandbox = await self.sandbox._connect(request.sandbox_id)
        await install_skills(sandbox)
        await sandbox.files.write(
            REVIEW_FIX_PROMPT_PATH,
            build_review_fix_prompt(request, self.sandbox.settings.test_command),
        )
        result = await self.sandbox._run_claude(
            sandbox, review_fix_command(), IMPLEMENT_COMMAND_TIMEOUT_S
        )
        return parse_result(result.stdout)
