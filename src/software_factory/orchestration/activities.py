import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any

from e2b import AsyncSandbox, CommandExitException, FileNotFoundException, TimeoutException
from temporalio import activity
from temporalio.exceptions import ApplicationError

from software_factory.config import FactorySettings
from software_factory.github.client import TokenProvider
from software_factory.orchestration.notify_activities import UsageAlertNotifier
from software_factory.orchestration.token_store import SessionRecord, TokenStore
from software_factory.orchestration.token_usage import TokenMeter, stage_for_workflow
from software_factory.orchestration.usage import UsageWatcher
from software_factory.orchestration.models import (
    AnalyzeRequest,
    CloneBranchRequest,
    Clarification,
    CommitImplementationRequest,
    CommitPlanRequest,
    FixAnalysis,
    FixRequest,
    ImplementRequest,
    PlanIssueInput,
    PlanRequest,
    TestRunResult,
)

REPO_DIR = "/home/user/repo"
PROMPT_PATH = "/home/user/prompt.md"
IMPLEMENT_PROMPT_PATH = "/home/user/implement-prompt.md"
ANALYZE_PROMPT_PATH = "/home/user/analyze-prompt.md"
FIX_PROMPT_PATH = "/home/user/fix-prompt.md"
# Arquivos da análise da falha, fora do repo (nunca commitados).
FIX_DIR = "/home/user/fix"
ANALYSIS_PATH = f"{FIX_DIR}/analysis.md"
FIX_PLAN_PATH = f"{FIX_DIR}/plan.md"
QUESTION_PATH = f"{FIX_DIR}/question.md"
# Skills do superpowers (writing-plans, systematic-debugging) vendorizadas, sem instalar plugin via rede.
SKILLS_SOURCE_DIR = Path(__file__).parent / "skills"
SKILLS_DEST_DIR = "/home/user/.claude/skills"
PLANS_DIR = "docs/superpowers/plans"
# O identifier vai para caminho e comando shell: só o formato do Linear (ENG-12).
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+-[0-9]+")
# Hobby do E2B permite ~1h contínua; o sandbox precisa durar mais que qualquer activity.
SANDBOX_TIMEOUT_S = 55 * 60
PLAN_COMMAND_TIMEOUT_S = 40 * 60
# Implementação + testes cabem na vida do sandbox junto com clone e commit.
IMPLEMENT_COMMAND_TIMEOUT_S = 30 * 60
TEST_COMMAND_TIMEOUT_S = 10 * 60
ANALYZE_COMMAND_TIMEOUT_S = 25 * 60
TEST_OUTPUT_TAIL = 4000
HEARTBEAT_INTERVAL_S = 30
# Teto para as notificações de uso terminarem antes da activity devolver.
NOTIFY_WAIT_S = 30
GIT_COMMAND_TIMEOUT_S = 60
CLONE_BRANCH_DEPTH = 50
# commit/push com hooks desligados: hooks plantados em .git/hooks (o Claude teve Bash) não rodam
# com o token no ambiente.
GIT_NO_HOOKS = "git -c core.hooksPath=/dev/null"
GIT_AUTHOR_NAME = "software-factory"
GIT_AUTHOR_EMAIL = "software-factory@users.noreply.github.com"


def plan_path(identifier: str) -> str:
    """Caminho do plano relativo ao repo; rejeita identifiers fora do formato do Linear."""
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ApplicationError(f"identifier inválido: {identifier!r}", non_retryable=True)
    return f"{PLANS_DIR}/{identifier}.md"


def plan_branch(identifier: str) -> str:
    plan_path(identifier)
    return f"factory/{identifier}"


def plan_command(identifier: str) -> str:
    # A regra Edit(...) cobre também a ferramenta Write; o Claude só escreve o plano.
    return (
        f"cat {PROMPT_PATH} | claude -p --output-format stream-json --verbose"
        f' --allowedTools "Read,Grep,Glob,Skill,Edit({plan_path(identifier)})"'
    )


def build_plan_prompt(issue: PlanIssueInput) -> str:
    path = plan_path(issue.identifier)
    return f"""Use a skill `writing-plans` para escrever o plano de implementação da issue abaixo.

- A issue é a spec: no campo **Spec:** do cabeçalho, use "Issue {issue.identifier} ({issue.url or "-"})".
- Salve o plano exatamente em `{path}` (substitui o caminho padrão da skill).
- Não altere nenhum outro arquivo.
- Pule a seção "Execution Handoff": não faça perguntas; termine assim que o plano
  estiver salvo e revisado.

## Issue {issue.identifier}: {issue.title}

URL: {issue.url or "-"}

{issue.description or "(sem descrição)"}
"""


def implement_command() -> str:
    return (
        f"cat {IMPLEMENT_PROMPT_PATH} | claude -p --output-format stream-json --verbose"
        ' --allowedTools "Read,Grep,Glob,Edit,Write,Bash"'
    )


def build_implement_prompt(issue: PlanIssueInput, test_command: str) -> str:
    path = plan_path(issue.identifier)
    return f"""Implemente a issue abaixo seguindo o plano de implementação em `{path}`.

- Leia o plano inteiro e implemente todas as tarefas, na ordem, seguindo os passos de cada
  uma (TDD: teste primeiro, ver falhar, implementar, ver passar).
- Os testes do repo rodam com `{test_command}`; rode até ficar verde antes de terminar.
- Faça um commit local por tarefa concluída.
- **Não** faça `git push` nem mexa em remotes; o push é feito depois, fora daqui.
- Pode haver trabalho parcial de uma tentativa anterior: confira `git log` e `git status`
  antes de começar e continue de onde parou.
- Não faça perguntas; termine com um resumo do que foi implementado.

## Issue {issue.identifier}: {issue.title}

URL: {issue.url or "-"}

{issue.description or "(sem descrição)"}
"""


_NO_TEST_TAMPERING = (
    "Proibido apagar, pular ou afrouxar testes (nada de `skip`, `xfail`, remover asserts) "
    "e proibido mudar o `TEST_COMMAND`: a correção tem de estar no código, não nos testes."
)


def _fence(text: str) -> str:
    # Cerca mais longa que qualquer sequência de crases da saída, para não fechar o bloco antes.
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _issue_section(issue: PlanIssueInput) -> str:
    return f"""## Issue {issue.identifier}: {issue.title}

URL: {issue.url or "-"}

{issue.description or "(sem descrição)"}
"""


def _clarifications_section(clarifications: tuple[Clarification, ...]) -> str:
    if not clarifications:
        return ""
    items = "\n\n".join(f"- Pergunta: {c.question}\n  Resposta: {c.answer}" for c in clarifications)
    return f"## Decisões da pessoa\n\n{items}\n\n"


def analyze_command() -> str:
    return (
        f"cat {ANALYZE_PROMPT_PATH} | claude -p --output-format stream-json --verbose"
        ' --allowedTools "Read,Grep,Glob,Skill,Edit,Write,Bash"'
    )


def fix_command() -> str:
    return (
        f"cat {FIX_PROMPT_PATH} | claude -p --output-format stream-json --verbose"
        ' --allowedTools "Read,Grep,Glob,Skill,Edit,Write,Bash"'
    )


def build_analyze_prompt(request: AnalyzeRequest, test_command: str) -> str:
    issue = request.issue
    attempts = ""
    if request.attempts:
        items = "\n\n".join(
            f"### Tentativa #{a.number}\n\nAnálise:\n\n{a.analysis}\n\n"
            f"Saída dos testes depois da correção:\n\n{_fence(a.test_output)}"
            for a in request.attempts
        )
        attempts = f"## Tentativas anteriores\n\n{items}\n\n"
    return f"""Os testes do repo falharam depois da implementação da issue abaixo. Ache a causa raiz e planeje a correção.

1. Use a skill `systematic-debugging` para achar a **causa raiz** da falha, seguindo as fases de
   investigação. Pode rodar `{test_command}` e qualquer outro comando. **Não corrija nada no
   repo**: as mudanças feitas aqui são descartadas.
2. Salve a análise (causa raiz, evidências e decisões tomadas) em `{ANALYSIS_PATH}`.
3. Depois use a skill `writing-plans` para escrever o plano de correção em `{FIX_PLAN_PATH}`
   (substitui o caminho padrão da skill). No campo **Spec:** do cabeçalho, use `{ANALYSIS_PATH}`
   e a issue {issue.identifier}. Pule a seção "Execution Handoff".
4. Decida sozinho e registre a decisão na análise. **Só** se a correção exigir uma decisão grande
   (mudar o escopo ou os requisitos da issue, mudar a arquitetura do plano original
   `{plan_path(issue.identifier)}`, mudar o comportamento esperado por testes existentes,
   adicionar dependência, migração de dados), escreva em `{QUESTION_PATH}` a pergunta com as
   opções e a sua recomendação, e **não** escreva o plano de correção.
5. {_NO_TEST_TAMPERING} Nenhuma proposta de correção pode incluir isso.
6. Não faça perguntas interativas; termine com um resumo.

## Saída dos testes (`{test_command}`)

{_fence(request.test_output)}

{attempts}{_clarifications_section(request.clarifications)}{_issue_section(issue)}"""


def build_fix_prompt(request: FixRequest, test_command: str) -> str:
    return f"""Implemente a correção dos testes que falharam na issue abaixo, seguindo o plano em
`{FIX_PLAN_PATH}` (a análise da causa raiz está em `{ANALYSIS_PATH}`).

- Leia o plano e a análise inteiros e implemente as tarefas na ordem, com TDD (teste primeiro,
  ver falhar, implementar, ver passar).
- Os testes do repo rodam com `{test_command}`; rode até ficar verde antes de terminar.
- Faça um commit local por tarefa concluída.
- **Não** faça `git push` nem mexa em remotes; o push é feito depois, fora daqui.
- {_NO_TEST_TAMPERING}
- Respeite as decisões da pessoa listadas abaixo, se houver.
- Não faça perguntas; termine com um resumo do que foi corrigido.

{_clarifications_section(request.clarifications)}{_issue_section(request.issue)}"""


async def install_skills(sandbox: Any) -> None:
    """Copia as skills vendorizadas para ~/.claude/skills/ do sandbox."""
    for source in sorted(SKILLS_SOURCE_DIR.rglob("*")):
        if source.is_dir() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(SKILLS_SOURCE_DIR).as_posix()
        await sandbox.files.write(f"{SKILLS_DEST_DIR}/{relative}", source.read_text())


def parse_result(stdout: str) -> str:
    """Extrai o texto do evento `result` do stream-json do Claude Code."""
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    if result is None:
        raise ApplicationError("Claude Code terminou sem evento result")
    if result.get("subtype") != "success" or result.get("is_error") or not result.get("result"):
        raise ApplicationError(f"Claude Code falhou: {result.get('subtype')}")
    return result["result"]


async def _run_with_heartbeat(
    sandbox: Any, cmd: str, timeout: int, on_line: Callable[[str], None] | None = None
) -> Any:
    """Roda um comando longo em REPO_DIR mantendo o heartbeat da activity.

    `on_line` recebe cada chunk do stdout (não necessariamente linhas inteiras)."""

    def on_stdout(chunk: str) -> None:
        activity.heartbeat()
        if on_line:
            on_line(chunk)

    # O comando pode ficar muito tempo sem emitir nada; heartbeat periódico além do stdout.
    async def keep_alive() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    keep_alive_task = asyncio.create_task(keep_alive())
    try:
        return await sandbox.commands.run(
            cmd,
            cwd=REPO_DIR,
            on_stdout=on_stdout,
            timeout=timeout,
        )
    finally:
        keep_alive_task.cancel()


class SandboxActivities:
    def __init__(
        self,
        settings: FactorySettings,
        tokens: TokenProvider,
        sandbox_cls: Any = AsyncSandbox,
        usage_notifier: UsageAlertNotifier | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        self.settings = settings
        self.tokens = tokens
        self.sandbox_cls = sandbox_cls
        self.usage_notifier = usage_notifier
        self.token_store = token_store

    async def _run_claude(self, sandbox: Any, cmd: str, timeout: int) -> Any:
        """Roda uma sessão do Claude, avisa quando o uso da sessão passa do limiar e grava os tokens."""
        notifier = self.usage_notifier
        info = activity.info()
        watcher = UsageWatcher(f"{info.workflow_id}/{info.activity_type}") if notifier else None
        meter = TokenMeter()
        tasks: set[asyncio.Task[bool]] = set()

        def on_line(chunk: str) -> None:
            # Exceção aqui faria o E2B parar de ler o stdout e falhar a sessão.
            try:
                meter.feed(chunk)
            except Exception:
                activity.logger.warning("falha ao contar os tokens da sessão", exc_info=True)
            if watcher is None or notifier is None:
                return
            try:
                for alert in watcher.feed(chunk):
                    tasks.add(asyncio.create_task(notifier.notify(alert)))
            except Exception:
                activity.logger.warning("falha ao ler o uso da sessão", exc_info=True)

        try:
            return await _run_with_heartbeat(sandbox, cmd, timeout, on_line=on_line)
        finally:
            await self._record_session(meter, info)
            if tasks:
                # O aviso nunca derruba a sessão: erros e demora ficam só no log.
                done, pending = await asyncio.wait(tasks, timeout=NOTIFY_WAIT_S)
                for task in pending:
                    task.cancel()
                for task in done:
                    if not task.cancelled() and task.exception():
                        activity.logger.warning(
                            "aviso de uso falhou: %r", task.exception()
                        )

    async def _record_session(self, meter: TokenMeter, info: Any) -> None:
        """Grava o uso da sessão; nunca levanta (timeout e erro da sessão também gravam)."""
        try:
            stage = stage_for_workflow(info.workflow_id)
            usage = meter.usage()
            if self.token_store is None or stage is None or usage is None:
                return
            record = SessionRecord(
                key=uuid.uuid4().hex,
                issue_id=stage[1],
                stage=stage[0],
                workflow_id=info.workflow_id,
                run_id=info.workflow_run_id,
                activity=info.activity_type,
                attempt=info.attempt,
                recorded_at=datetime.now(UTC).isoformat(),
                usage=usage,
            )
            await asyncio.to_thread(self.token_store.record, record)
        except Exception:
            activity.logger.warning("falha ao gravar o uso de tokens da sessão", exc_info=True)

    async def _connect(self, sandbox_id: str) -> Any:
        return await self.sandbox_cls.connect(sandbox_id, api_key=self.settings.e2b_api_key)

    @activity.defn
    async def create_sandbox(self, issue_id: str) -> str:
        # Mesma chave em retry da mesma activity (mesmo sandbox); sessão e execução novas têm outra.
        # O E2B só aceita [a-z0-9-]: junta com hífen e troca o resto por hífen.
        info = activity.info()
        key = f"{info.workflow_id}-{info.workflow_run_id}-{info.activity_id}".lower()
        key = re.sub(r"[^a-z0-9-]", "-", key)
        sandbox = await self.sandbox_cls.create(
            template=self.settings.e2b_template,
            timeout=SANDBOX_TIMEOUT_S,
            envs=self.settings.claude_auth_env,
            metadata={"issue_id": issue_id},
            idempotency_key=key,
            api_key=self.settings.e2b_api_key,
        )
        activity.logger.info("sandbox criado: %s", sandbox.sandbox_id)
        return sandbox.sandbox_id

    @activity.defn
    async def clone_repo(self, sandbox_id: str) -> None:
        repo = self.settings.target_repo
        sandbox = await self._connect(sandbox_id)
        if not await sandbox.files.exists(f"{REPO_DIR}/.git"):
            await sandbox.commands.run(
                f"git clone --depth 1 {self._authed_url(repo)} {REPO_DIR}",
                envs=await self._token_env(repo),
                timeout=5 * 60,
            )
        # Também em retry: o Claude roda com Bash e não pode achar o token em .git/config.
        await sandbox.commands.run(
            f"git remote set-url origin https://github.com/{repo}.git",
            cwd=REPO_DIR,
            timeout=GIT_COMMAND_TIMEOUT_S,
        )

    @activity.defn
    async def generate_plan(self, request: PlanRequest) -> str:
        path = plan_path(request.issue.identifier)
        sandbox = await self._connect(request.sandbox_id)
        await install_skills(sandbox)
        # Plano de uma tentativa anterior: o Write do Claude exigiria Read antes; recomeça limpo.
        if await sandbox.files.exists(f"{REPO_DIR}/{path}"):
            await sandbox.files.remove(f"{REPO_DIR}/{path}")
        await sandbox.files.write(PROMPT_PATH, build_plan_prompt(request.issue))
        result = await self._run_claude(
            sandbox, plan_command(request.issue.identifier), PLAN_COMMAND_TIMEOUT_S
        )
        # O texto final do Claude é só um resumo; o plano é o arquivo salvo.
        parse_result(result.stdout)
        try:
            return await sandbox.files.read(f"{REPO_DIR}/{path}")
        except FileNotFoundException:
            raise ApplicationError(f"Claude Code não salvou o plano em {path}") from None

    @activity.defn
    async def commit_plan(self, request: CommitPlanRequest) -> str:
        path = plan_path(request.identifier)
        branch = plan_branch(request.identifier)
        repo = self.settings.target_repo
        sandbox = await self._connect(request.sandbox_id)
        if not await sandbox.files.exists(f"{REPO_DIR}/{path}"):
            raise ApplicationError(f"plano não encontrado em {path}")
        token_env = await self._token_env(repo)
        url = self._authed_url(repo)

        async def git(cmd: str, **kwargs: Any) -> str:
            result = await sandbox.commands.run(
                cmd, cwd=REPO_DIR, timeout=GIT_COMMAND_TIMEOUT_S, **kwargs
            )
            return result.stdout.strip()

        # O origin não guarda token: ls-remote e push usam a URL autenticada (token em envs=).
        await git(f"git checkout -B {branch}")
        await git(f"git add {path}")
        await git(
            f"git diff --cached --quiet || {GIT_NO_HOOKS}"
            f' -c user.name="{GIT_AUTHOR_NAME}" -c user.email="{GIT_AUTHOR_EMAIL}"'
            f' commit -m "docs: plano de implementação {request.identifier}"'
        )
        local_sha = await git("git rev-parse HEAD")
        remote = await git(f"git ls-remote --heads {url} refs/heads/{branch}", envs=token_env)
        remote_sha = remote.split()[0] if remote else ""
        if remote_sha == local_sha:
            return branch  # retry depois de um push que já aconteceu
        if remote_sha:
            raise ApplicationError(
                f"branch {branch} já existe no remoto com outro conteúdo", non_retryable=True
            )
        await git(f"{GIT_NO_HOOKS} push {url} HEAD:refs/heads/{branch}", envs=token_env)
        return branch

    @staticmethod
    def _authed_url(repo: str) -> str:
        # $GITHUB_TOKEN é expandido pelo shell a partir de envs=; o token nunca vai no comando.
        return f'"https://x-access-token:$GITHUB_TOKEN@github.com/{repo}.git"'

    async def _token_env(self, repo: str) -> dict[str, str]:
        return {"GITHUB_TOKEN": await self.tokens(repo)}

    @activity.defn
    async def clone_branch(self, request: CloneBranchRequest) -> str:
        """Clona factory/<ID> sem deixar o token no origin; retorna o SHA clonado."""
        branch = plan_branch(request.identifier)
        repo = self.settings.target_repo
        sandbox = await self._connect(request.sandbox_id)
        if not await sandbox.files.exists(f"{REPO_DIR}/.git"):
            # Histórico recente para a análise ver o que mudou (sem token no origin, não dá
            # para aprofundar depois).
            await sandbox.commands.run(
                f"git clone --depth {CLONE_BRANCH_DEPTH} --branch {branch}"
                f" {self._authed_url(repo)} {REPO_DIR}",
                envs=await self._token_env(repo),
                timeout=5 * 60,
            )

        async def git(cmd: str) -> str:
            result = await sandbox.commands.run(cmd, cwd=REPO_DIR, timeout=GIT_COMMAND_TIMEOUT_S)
            return result.stdout.strip()

        # Também em retry: o Claude roda com Bash e não pode achar o token em .git/config.
        await git(f"git remote set-url origin https://github.com/{repo}.git")
        await git(f'git config user.name "{GIT_AUTHOR_NAME}"')
        await git(f'git config user.email "{GIT_AUTHOR_EMAIL}"')
        return await git(f"git rev-parse refs/remotes/origin/{branch}")

    @activity.defn
    async def implement_plan(self, request: ImplementRequest) -> str:
        sandbox = await self._connect(request.sandbox_id)
        await sandbox.files.write(
            IMPLEMENT_PROMPT_PATH,
            build_implement_prompt(request.issue, self.settings.test_command),
        )
        result = await self._run_claude(
            sandbox, implement_command(), IMPLEMENT_COMMAND_TIMEOUT_S
        )
        return parse_result(result.stdout)

    @activity.defn
    async def analyze_failure(self, request: AnalyzeRequest) -> FixAnalysis:
        """Acha a causa raiz da falha e escreve o plano de correção (ou uma pergunta)."""
        sandbox = await self._connect(request.sandbox_id)
        await install_skills(sandbox)
        # Retry e sessões novas não herdam arquivos velhos de análise.
        await sandbox.commands.run(
            f"rm -rf {FIX_DIR} && mkdir -p {FIX_DIR}", timeout=GIT_COMMAND_TIMEOUT_S
        )
        await sandbox.files.write(
            ANALYZE_PROMPT_PATH, build_analyze_prompt(request, self.settings.test_command)
        )
        result = await self._run_claude(sandbox, analyze_command(), ANALYZE_COMMAND_TIMEOUT_S)
        parse_result(result.stdout)

        async def read_optional(path: str) -> str:
            try:
                return (await sandbox.files.read(path)).strip()
            except FileNotFoundException:
                return ""

        analysis = await read_optional(ANALYSIS_PATH)
        question = await read_optional(QUESTION_PATH)
        if question:
            return FixAnalysis(analysis=analysis, question=question)
        plan = await read_optional(FIX_PLAN_PATH)
        if not analysis or not plan:
            raise ApplicationError(
                f"Claude Code não salvou a análise em {ANALYSIS_PATH} e o plano em {FIX_PLAN_PATH}"
            )
        return FixAnalysis(analysis=analysis, plan=plan)

    @activity.defn
    async def implement_fix(self, request: FixRequest) -> str:
        sandbox = await self._connect(request.sandbox_id)
        await install_skills(sandbox)
        await sandbox.files.write(ANALYSIS_PATH, request.analysis)
        await sandbox.files.write(FIX_PLAN_PATH, request.plan)
        await sandbox.files.write(
            FIX_PROMPT_PATH, build_fix_prompt(request, self.settings.test_command)
        )
        result = await self._run_claude(sandbox, fix_command(), IMPLEMENT_COMMAND_TIMEOUT_S)
        return parse_result(result.stdout)

    @activity.defn
    async def run_tests(self, sandbox_id: str) -> TestRunResult:
        """Roda TEST_COMMAND no repo; o orquestrador não confia no relato do Claude."""
        sandbox = await self._connect(sandbox_id)
        try:
            result = await _run_with_heartbeat(
                sandbox, self.settings.test_command, TEST_COMMAND_TIMEOUT_S
            )
        except CommandExitException as error:
            output = (error.stdout or "") + (error.stderr or "")
            return TestRunResult(passed=False, output=output[-TEST_OUTPUT_TAIL:])
        except TimeoutException as error:
            # Timeout é vermelho, não falha: alimenta o loop de correção (teste travado/lento).
            return TestRunResult(
                passed=False,
                output=(
                    f"TEST_COMMAND estourou o timeout de {TEST_COMMAND_TIMEOUT_S // 60} min"
                    f" ({error}); algum teste travou ou ficou lento demais."
                ),
            )
        output = (result.stdout or "") + (getattr(result, "stderr", "") or "")
        return TestRunResult(passed=True, output=output[-TEST_OUTPUT_TAIL:])

    @activity.defn
    async def commit_implementation(self, request: CommitImplementationRequest) -> str:
        branch = plan_branch(request.identifier)
        sandbox = await self._connect(request.sandbox_id)
        repo = self.settings.target_repo
        token_env = await self._token_env(repo)
        url = self._authed_url(repo)

        async def git(cmd: str, **kwargs: Any) -> str:
            result = await sandbox.commands.run(
                cmd, cwd=REPO_DIR, timeout=GIT_COMMAND_TIMEOUT_S, **kwargs
            )
            return result.stdout.strip()

        message = request.message or f"feat: implementa {request.identifier}"
        await git("git add -A")
        await git(f'git diff --cached --quiet || {GIT_NO_HOOKS} commit -m "{message}"')
        local_sha = await git("git rev-parse HEAD")
        if local_sha == request.base_sha:
            if request.require_changes:
                raise ApplicationError("Claude Code não fez mudanças", non_retryable=True)
            return branch
        remote = await git(
            f"git ls-remote --heads {url} refs/heads/{branch}", envs=token_env
        )
        remote_sha = remote.split()[0] if remote else ""
        if remote_sha == local_sha:
            return branch  # retry depois de um push que já aconteceu
        if remote_sha != request.base_sha:
            raise ApplicationError(
                f"branch {branch} mudou no remoto desde o clone", non_retryable=True
            )
        await git(
            f"{GIT_NO_HOOKS} push {url} HEAD:refs/heads/{branch}", envs=token_env
        )
        return branch

    @activity.defn
    async def kill_sandbox(self, sandbox_id: str) -> None:
        # kill retorna False se o sandbox já não existe — idempotente.
        await self.sandbox_cls.kill(sandbox_id, api_key=self.settings.e2b_api_key)
