# Loop de correção com testes vermelhos — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando `TEST_COMMAND` fica vermelho no `ImplementIssueWorkflow`, em vez de falhar, o workflow entra num loop **analisar → plano de correção → implementar → testar** até ficar verde, e só pergunta à pessoa (comentário no Linear, esperando a resposta na thread) quando há uma decisão grande que o Claude não deve tomar sozinho.

**Architecture:** Cada etapa longa roda num sandbox E2B próprio ("sessão"), porque o loop e a espera por resposta humana não cabem nos ~55 min de vida de um sandbox. O estado entre sessões vive no git: ao fim de cada sessão de implementação vermelha, a activity faz push de um commit `wip:` em `factory/<ID>` (a PR continua só abrindo com verde), e a próxima sessão clona a branch de novo. A sessão de análise roda o Claude com as skills `systematic-debugging` (causa raiz primeiro) e `writing-plans` (plano de correção), devolvendo análise + plano **ou** uma pergunta; o workflow guarda análise/plano no próprio estado e os entrega à sessão de correção. Perguntas viram comentário no Linear; a resposta chega como reply nessa thread → webhook `Comment` → signal `human_answer` no workflow.

**Tech Stack:** Python 3.12, temporalio (signals, `workflow.wait_condition`, `workflow.uuid4`), e2b `AsyncSandbox`, httpx (GraphQL do Linear), FastAPI, unittest. Nenhuma dependência nova.

**Spec:** pedido do usuário em 2026-11-14: "when the tests do not go green, instead of just failing, we should implement a loop that tries to fix it and only stops when everything goes green, instead of going straight to the fix, first we should analyze the problem, then create a fix implementation plan, then implement and check if everything goes green. If theres is a big decision that must be made u can add a comment to the issue in linear and wait for my response (only in big changes that must be made, try to make decision on your on and only use this as a last resourse)."

## Decisões tomadas neste plano (não estão na spec)

- **Checkpoint vermelho em `factory/<ID>`:** muda a regra atual "só com verde faz push". A regra passa a ser "só com verde abre a PR". Sem isso o trabalho de uma sessão se perde quando o sandbox morre.
- **Um sandbox por sessão**, não por workflow: análise e correção ficam em sandboxes separados, cada um com o orçamento inteiro de 55 min. Muda o item "Um sandbox por execução de workflow" do CLAUDE.md.
- **Trava de custo:** depois de `MAX_FIX_ATTEMPTS = 5` correções seguidas sem verde, o próprio workflow pergunta no Linear como seguir (conta como "decisão grande"). A resposta zera o contador e o loop continua. Assim o loop "só para no verde" sem gastar sem limite.
- **Como parar:** mover a issue para fora de `In Progress`. O workflow confere o status antes de cada tentativa e, durante a espera, a cada `ANSWER_POLL_INTERVAL = 1h`. Sai com `OUTCOME_STOPPED`, sem PR.
- **Resposta = reply na thread do comentário da pergunta** (`parentId` == id do comentário). A personal API key é da própria pessoa, então o autor não distingue pergunta de resposta; o `parentId` distingue.
- **Sem resposta em `HUMAN_ANSWER_TIMEOUT = 7 dias`**: o workflow falha (sem compensação, igual hoje).
- **Skill `systematic-debugging` do superpowers 7.0.2 vendorizada** ao lado da `writing-plans` (mesmo pacote, MIT).

## Global Constraints

- Nenhuma dependência nova. Linear só via GraphQL com `LinearClient` (`httpx`).
- Workflow determinístico: id do comentário com `workflow.uuid4()`, prazos com `workflow.now()`, espera com `workflow.wait_condition(..., timeout=...)`.
- Orçamento por sandbox (`SANDBOX_TIMEOUT_S = 55 * 60`): sessão de análise = clone + `analyze_failure` (comando `ANALYZE_COMMAND_TIMEOUT_S = 25 * 60`, activity 30 min, `maximum_attempts=1`); sessão de correção = clone + `implement_fix` (comando `IMPLEMENT_COMMAND_TIMEOUT_S`, activity 35 min, `maximum_attempts=1`) + `run_tests` + `commit_implementation` (mesmos timeouts de hoje).
- Constantes do workflow (em `implement_workflow.py`): `MAX_FIX_ATTEMPTS = 5`, `ATTEMPTS_IN_PROMPT = 3` (só as 3 últimas tentativas vão para o prompt), `HUMAN_ANSWER_TIMEOUT = timedelta(days=7)`, `ANSWER_POLL_INTERVAL = timedelta(hours=1)`.
- Arquivos da análise fora do repo, em `FIX_DIR = "/home/user/fix"`: `ANALYSIS_PATH = f"{FIX_DIR}/analysis.md"`, `FIX_PLAN_PATH = f"{FIX_DIR}/plan.md"`, `QUESTION_PATH = f"{FIX_DIR}/question.md"`. Nunca commitados.
- Prompts de análise e de correção proíbem "corrigir" apagando, pulando (`skip`/`xfail`) ou enfraquecendo testes, e mudando `TEST_COMMAND`.
- Mensagens de commit: final `feat: implementa <ID>`; checkpoint `wip: tentativa <n> de <ID> (testes vermelhos)` (n = 0 para a implementação do plano, 1.. para correções).
- O token continua fora do `origin` (o Claude tem `Bash` nas sessões de análise e correção).
- Webhook do Linear precisa do recurso **Comments** habilitado, além de Issues.
- Logs, mensagens, prompts e docs em português.

## Review Focus

1. O Claude "conserta" apagando/pulando testes ou afrouxando asserts: os dois prompts proíbem isso explicitamente. Teste em Task 2.
2. Resposta duplicada (Linear reenvia), reply a uma pergunta antiga ou comentário fora da thread: ignorados, só a primeira resposta à pergunta pendente vale. Testes em Task 4 (workflow) e Task 5 (webhook).
3. Retry de `post_comment` depois de o comentário já ter sido criado: não duplica (consulta pelo id gerado no workflow antes de criar). Teste em Task 3.
4. Sessão de correção em que o Claude não muda nada e os testes seguem vermelhos: conta como tentativa e o loop segue, sem o erro "Claude Code não fez mudanças". Testes em Task 1 e Task 4.
5. Teste flaky que fica verde sem mudança depois de checkpoints já empurrados: o commit final não falha com "não fez mudanças" (`require_changes` só vale enquanto nada foi empurrado). Testes em Task 1 e Task 4.

---

### Task 1: Modelos e sandbox por sessão (checkpoint vermelho)

**Files:**
- Modify: `src/software_factory/orchestration/models.py`
- Modify: `src/software_factory/orchestration/activities.py` (`create_sandbox`, `commit_implementation`)
- Test: `tests/test_activities.py`

**Interfaces:**
- Produces (em `models.py`):

```python
@dataclass(frozen=True)
class CommitImplementationRequest:
    sandbox_id: str
    identifier: str
    base_sha: str
    message: str = ""            # "" → f"feat: implementa {identifier}"
    require_changes: bool = True  # False: sem mudanças retorna a branch sem push

@dataclass(frozen=True)
class FixAttempt:
    number: int
    analysis: str
    test_output: str  # saída dos testes depois da correção

@dataclass(frozen=True)
class Clarification:
    question: str
    answer: str

@dataclass(frozen=True)
class AnalyzeRequest:
    sandbox_id: str
    issue: PlanIssueInput
    test_output: str
    attempts: tuple[FixAttempt, ...] = ()
    clarifications: tuple[Clarification, ...] = ()

@dataclass(frozen=True)
class FixAnalysis:
    analysis: str
    plan: str | None = None
    question: str | None = None  # preenchida = decisão grande; plan fica None

@dataclass(frozen=True)
class FixRequest:
    sandbox_id: str
    issue: PlanIssueInput
    analysis: str
    plan: str
    clarifications: tuple[Clarification, ...] = ()

@dataclass(frozen=True)
class CommentRequest:
    issue_id: str
    comment_id: str  # UUID v4 gerado no workflow: idempotência
    body: str

@dataclass(frozen=True)
class HumanAnswer:
    question_id: str  # id do comentário da pergunta (parentId do reply)
    comment_id: str
    body: str

OUTCOME_STOPPED = "stopped"
```

- Produces: `create_sandbox` usa `idempotency_key=f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}"` — o mesmo sandbox em retry da mesma activity, um novo por sessão e por nova execução do workflow.

- [ ] **Step 1: Write the failing tests** em `TestSandboxActivities`

```python
async def test_create_sandbox(self) -> None:  # atualiza o existente
    ...
    info = self.env.info
    self.assertEqual(
        call["idempotency_key"],
        f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}",
    )

async def test_commit_implementation_checkpoint_message(self) -> None:
    # mesmo arranjo de test_commit_implementation_pushes, com
    # message="wip: tentativa 1 de ENG-12 (testes vermelhos)", require_changes=False
    commit = next(r["cmd"] for r in runs if "git commit" in r["cmd"])
    self.assertIn('-m "wip: tentativa 1 de ENG-12 (testes vermelhos)"', commit)
    self.assertTrue(any("git push" in r["cmd"] for r in runs))

async def test_commit_implementation_no_changes_allowed(self) -> None:
    # HEAD == base_sha, require_changes=False
    self.assertEqual(result, "factory/ENG-12")
    self.assertFalse(any("git push" in r["cmd"] for r in runs))
    self.assertFalse(any("ls-remote" in r["cmd"] for r in runs))
```

`test_commit_implementation_rejects_no_changes` (default `require_changes=True`) continua como está.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: FAIL nos três testes acima (`TypeError` de argumento inesperado / chave de idempotência diferente).

- [ ] **Step 3: Implement** os modelos acima em `models.py`; em `commit_implementation`, `message = request.message or f"feat: implementa {request.identifier}"`; se `local_sha == request.base_sha`: `require_changes` → `ApplicationError` não-retentável (como hoje), senão `return branch` sem `ls-remote`/push. `create_sandbox` com a nova chave.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: PASS (todos)

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/models.py src/software_factory/orchestration/activities.py tests/test_activities.py
git commit -m "feat: modelos do loop de correção e checkpoint vermelho no commit"
```

---

### Task 2: Activities `analyze_failure` e `implement_fix`

**Files:**
- Create: `src/software_factory/orchestration/skills/systematic-debugging/` (cópia de `~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.2/skills/systematic-debugging/`: `SKILL.md`, `root-cause-tracing.md`, `defense-in-depth.md`, `condition-based-waiting.md`, `condition-based-waiting-example.ts`, `find-polluter.sh`) + `LICENSE` (copiar de `skills/writing-plans/LICENSE`)
- Modify: `src/software_factory/orchestration/activities.py`
- Test: `tests/test_activities.py`

**Interfaces:**
- Consumes: `AnalyzeRequest`, `FixAnalysis`, `FixRequest`, `FixAttempt`, `Clarification` (Task 1); `parse_result`, `_run_with_heartbeat`, `plan_path` existentes.
- Produces:
  - `SKILLS_SOURCE_DIR = Path(__file__).parent / "skills"`, `SKILLS_DEST_DIR = "/home/user/.claude/skills"`; `async def install_skills(sandbox: Any) -> None` escreve cada arquivo de `SKILLS_SOURCE_DIR.rglob("*")` em `SKILLS_DEST_DIR/<relativo>`. `generate_plan` passa a usá-la (substitui `SKILL_SOURCE`/`SKILL_DEST`).
  - `def build_analyze_prompt(request: AnalyzeRequest, test_command: str) -> str`
  - `def build_fix_prompt(request: FixRequest, test_command: str) -> str`
  - `def analyze_command() -> str` / `def fix_command() -> str`: `cat <prompt> | claude -p --output-format stream-json --verbose --allowedTools "Read,Grep,Glob,Skill,Edit,Write,Bash"`, com prompts em `ANALYZE_PROMPT_PATH = "/home/user/analyze-prompt.md"` e `FIX_PROMPT_PATH = "/home/user/fix-prompt.md"`.
  - `SandboxActivities.analyze_failure(request: AnalyzeRequest) -> FixAnalysis`
  - `SandboxActivities.implement_fix(request: FixRequest) -> str` (resumo do Claude)

Conteúdo obrigatório do **prompt de análise** (`build_analyze_prompt`):
1. Use a skill `systematic-debugging` para achar a **causa raiz** da falha (fases de investigação; pode rodar `{test_command}` e qualquer comando). Não corrija nada no repo: mudanças são descartadas.
2. Salve a análise (causa raiz, evidências, decisões tomadas) em `ANALYSIS_PATH`.
3. Depois use a skill `writing-plans` para escrever o plano de correção em `FIX_PLAN_PATH` (substitui o caminho padrão; Spec = `ANALYSIS_PATH` + a issue; pule "Execution Handoff").
4. Política de decisão: decida sozinho e registre a decisão na análise. **Só** se a correção exigir uma decisão grande — mudar o escopo/requisitos da issue, mudar a arquitetura do plano original `plan_path(id)`, mudar o comportamento esperado por testes existentes, adicionar dependência, migração de dados — escreva em `QUESTION_PATH` a pergunta com as opções e a sua recomendação, e **não** escreva o plano.
5. Proibido propor apagar, pular ou afrouxar testes, ou mudar `TEST_COMMAND`.
6. Seções: saída dos testes (bloco de código), tentativas anteriores (`#n`: análise + saída depois da correção), decisões da pessoa (pergunta/resposta), issue (título, URL, descrição). Seções vazias são omitidas.

Conteúdo obrigatório do **prompt de correção** (`build_fix_prompt`): implemente o plano em `FIX_PLAN_PATH` (análise em `ANALYSIS_PATH`), TDD; rode `{test_command}` até verde; commit local por tarefa; sem `git push`/remotes; mesma proibição de mexer em testes/`TEST_COMMAND`; respeite as decisões da pessoa (listadas); sem perguntas; termine com resumo.

- [ ] **Step 1: Write the failing tests**

```python
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

    def test_fix_prompt(self) -> None:
        prompt = build_fix_prompt(
            FixRequest("sbx-1", ISSUE, "análise", "plano",
                       (Clarification("Usar bcrypt?", "Sim"),)), "python -m pytest")
        for text in (FIX_PLAN_PATH, ANALYSIS_PATH, "python -m pytest", "git push",
                     "Usar bcrypt?", "apagar, pular ou afrouxar testes"):
            self.assertIn(text, prompt)
```

Em `TestSandboxActivities` (o `on_run` do `FakeCommands` simula o Claude gravando os arquivos):

```python
async def test_analyze_failure_returns_plan(self) -> None:
    # on_run grava ANALYSIS_PATH="causa" e FIX_PLAN_PATH="plano"; stdout = evento result success
    result = await self.env.run(self.activities.analyze_failure, AnalyzeRequest("sbx-1", ISSUE, "FAILED"))
    self.assertEqual(result, FixAnalysis(analysis="causa", plan="plano"))
    files = FakeSandbox.instance.files.written
    self.assertIn(f"{SKILLS_DEST_DIR}/systematic-debugging/SKILL.md", files)
    self.assertIn(f"{SKILLS_DEST_DIR}/writing-plans/SKILL.md", files)
    self.assertTrue(any(f"rm -rf {FIX_DIR}" in r["cmd"] for r in runs))  # limpa antes do Claude
    self.assertTrue(self.heartbeats)

async def test_analyze_failure_returns_question(self) -> None:
    # on_run grava ANALYSIS_PATH="causa" e QUESTION_PATH="Trocar o ORM?"
    self.assertEqual(result, FixAnalysis(analysis="causa", question="Trocar o ORM?"))

async def test_analyze_failure_without_plan_or_question(self) -> None:
    # on_run grava só ANALYSIS_PATH
    with self.assertRaises(ApplicationError): ...

async def test_implement_fix_writes_plan_and_runs_claude(self) -> None:
    result = await self.env.run(self.activities.implement_fix,
                                FixRequest("sbx-1", ISSUE, "causa", "plano"))
    files = FakeSandbox.instance.files.written
    self.assertEqual(files[FIX_PLAN_PATH], "plano")
    self.assertEqual(files[ANALYSIS_PATH], "causa")
    self.assertIn(FIX_PROMPT_PATH, files)
    self.assertEqual(result, "<texto do result>")
```

`test_generate_plan` passa a conferir `f"{SKILLS_DEST_DIR}/writing-plans/SKILL.md"` em vez de `SKILL_DEST`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: FAIL com `ImportError` (`build_analyze_prompt`, `ANALYSIS_PATH`, ...).

- [ ] **Step 3: Copy the skill** para `orchestration/skills/systematic-debugging/` (arquivos listados acima, sem alterações) + `LICENSE`.

- [ ] **Step 4: Implement** `install_skills`, prompts, comandos e as duas activities. `analyze_failure`: `install_skills`; `rm -rf {FIX_DIR} && mkdir -p {FIX_DIR}` (retry e sessões não herdam arquivos velhos); grava o prompt; `_run_with_heartbeat(..., ANALYZE_COMMAND_TIMEOUT_S)`; `parse_result`; pergunta não vazia em `QUESTION_PATH` → `FixAnalysis(analysis, question=...)` (análise vazia se o arquivo faltar); senão análise e plano obrigatórios, faltando → `ApplicationError`. `implement_fix`: `install_skills`, grava análise e plano nos caminhos fixos, prompt, `_run_with_heartbeat(..., IMPLEMENT_COMMAND_TIMEOUT_S)`, `parse_result`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/orchestration/skills/systematic-debugging src/software_factory/orchestration/activities.py tests/test_activities.py
git commit -m "feat: activities de análise da falha e de implementação da correção"
```

---

### Task 3: `LinearActivities.post_comment` idempotente

**Files:**
- Modify: `src/software_factory/orchestration/linear_activities.py`
- Test: `tests/test_linear_activities.py`

**Interfaces:**
- Consumes: `CommentRequest` (Task 1).
- Produces: `LinearActivities.post_comment(request: CommentRequest) -> str` (id do comentário).

GraphQL:

```graphql
query($issueId: String!, $commentId: ID!) {
  issue(id: $issueId) { comments(filter: {id: {eq: $commentId}}) { nodes { id } } }
}
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id } }
}
```

`input = {"id": comment_id, "issueId": issue_id, "body": body}`. Consulta primeiro: se o comentário já existe (retry depois de criado), retorna o id sem mutation. Issue inexistente → não-retentável; `success: false` → retentável.

- [ ] **Step 1: Write the failing tests** em `TestPostComment` (mesmo `MockTransport` de `TestMoveIssue`)

```python
async def test_creates_comment(self) -> None:
    self.responses = [{"data": {"issue": {"comments": {"nodes": []}}}},
                      {"data": {"commentCreate": {"success": True, "comment": {"id": "c-1"}}}}]
    result = await self.env.run(self.activities.post_comment, CommentRequest("issue-1", "c-1", "Pergunta?"))
    self.assertEqual(result, "c-1")
    variables = json.loads(self.requests[1].content)["variables"]
    self.assertEqual(variables["input"], {"id": "c-1", "issueId": "issue-1", "body": "Pergunta?"})

async def test_retry_after_create_is_noop(self) -> None:
    self.responses = [{"data": {"issue": {"comments": {"nodes": [{"id": "c-1"}]}}}}]
    self.assertEqual(result, "c-1")
    self.assertEqual(len(self.requests), 1)

async def test_missing_issue_is_non_retryable(self) -> None: ...   # issue: None
async def test_unsuccessful_create_raises(self) -> None: ...       # success False → retentável
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_linear_activities -v`
Expected: FAIL com `AttributeError: ... 'post_comment'`

- [ ] **Step 3: Implement** `post_comment` conforme acima.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.test_linear_activities -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/linear_activities.py tests/test_linear_activities.py
git commit -m "feat: activity post_comment idempotente no Linear"
```

---

### Task 4: Loop de correção no `ImplementIssueWorkflow`

**Files:**
- Modify: `src/software_factory/orchestration/implement_workflow.py`
- Modify: `src/software_factory/orchestration/worker.py` (registrar `analyze_failure`, `implement_fix`, `post_comment`)
- Test: `tests/test_implement_workflow.py`

**Interfaces:**
- Consumes: activities `analyze_failure`, `implement_fix` (Task 2), `post_comment` (Task 3), `commit_implementation` com `message`/`require_changes` (Task 1); modelos da Task 1.
- Produces: signal `ImplementIssueWorkflow.human_answer(answer: HumanAnswer) -> None` (usado pela Task 5); resultado novo `ImplementResult(identifier, OUTCOME_STOPPED)`.

Algoritmo (o resto — timeouts e retry — segue os Global Constraints):

```python
# estado da instância: _plan_sha, _clarifications: list, _pending_question: str | None, _answer: str | None

tests = await self._implement_session(issue, attempt=0)           # implement_plan
attempts: list[FixAttempt] = []
round_start = 0
while not tests.passed:
    if not await self._still_in_progress(issue):
        return ImplementResult(issue.identifier, OUTCOME_STOPPED)
    if len(attempts) - round_start >= MAX_FIX_ATTEMPTS:
        question = stuck_question(len(attempts) - round_start, attempts[-1], tests.output)
        if not await self._ask(issue, question):
            return ImplementResult(issue.identifier, OUTCOME_STOPPED)
        round_start = len(attempts)
    analysis = await self._analysis_session(issue, tests.output, attempts[-ATTEMPTS_IN_PROMPT:])
    if analysis.question:
        if not await self._ask(issue, analysis.question):
            return ImplementResult(issue.identifier, OUTCOME_STOPPED)
        continue                                                   # reanalisa com a resposta
    tests = await self._implement_session(issue, attempt=len(attempts) + 1, fix=analysis)
    attempts.append(FixAttempt(len(attempts) + 1, analysis.analysis, tests.output))
# verde: o commit final já foi empurrado pela última sessão → PR → In Review
```

- `_implement_session(issue, attempt, fix=None) -> TestRunResult`: `create_sandbox` → `try:` `clone_branch` (primeira vez guarda `_plan_sha`) → `implement_plan` (sem `fix`) ou `implement_fix(FixRequest(..., tuple(self._clarifications)))` → `run_tests` → `commit_implementation(message=f"feat: implementa {id}"` se verde, senão `f"wip: tentativa {attempt} de {id} (testes vermelhos)"`, `require_changes=tests.passed and base_sha == self._plan_sha)` → `finally: kill_sandbox`.
- `_analysis_session(...) -> FixAnalysis`: `create_sandbox` → `try:` `clone_branch` → `analyze_failure(AnalyzeRequest(..., tuple(attempts), tuple(self._clarifications)))` → `finally: kill_sandbox`.
- `_still_in_progress(issue)`: `move_issue(MoveIssueRequest(issue_id, to_state=STATE_IN_PROGRESS, from_state=STATE_IN_PROGRESS))`.
- `_ask(issue, question) -> bool` (False = issue saiu de `In Progress`): `comment_id = str(workflow.uuid4())`; `_pending_question = comment_id`, `_answer = None` **antes** de `post_comment(CommentRequest(issue_id, comment_id, format_question(question)))`; espera em fatias de `ANSWER_POLL_INTERVAL` até `HUMAN_ANSWER_TIMEOUT` (prazo via `workflow.now()`), chamando `_still_in_progress` a cada fatia vencida; prazo estourado → `ApplicationError(non_retryable=True)`; com resposta, `_clarifications.append(Clarification(question, answer))`, limpa `_pending_question` e retorna True.
- `human_answer`: aceita só se `answer.question_id == self._pending_question` e `self._answer is None`.
- `format_question(question)` (copy fixo):

```
**software-factory precisa de uma decisão** para seguir com a correção dos testes.

{question}

_Responda **nesta thread** (reply a este comentário) para o workflow continuar. Para parar, mova a issue para fora de In Progress._
```

- `stuck_question(n, last, output)`: `f"Fiz {n} tentativas de correção e os testes continuam vermelhos.\n\n**Última análise:**\n{last.analysis[:3000]}\n\n**Saída dos testes:**\n```\n{output[-2000:]}\n```\n\nComo devo seguir?"`

Fake: `FakeActivities` ganha `red_runs: int` (quantas execuções de `run_tests` vêm vermelhas antes do verde; substitui `tests_red`), `analyses: list[FixAnalysis]` (consumidas em ordem; default `FixAnalysis("causa", plan="plano")`), `answers: list[str]`, `stop_after_checks: int | None` (quantas checagens `from_state="In Progress"` **depois** da checagem inicial do workflow voltam True antes de voltar False; None = sempre True) e `client`. `clone_branch` retorna `f"sha-{n}"` (n = chamada). `post_comment` registra o pedido e, se houver resposta em `answers`, agenda `client.get_workflow_handle(activity.info().workflow_id).signal(ImplementIssueWorkflow.human_answer, HumanAnswer(request.comment_id, "reply-1", answers.pop(0)))` com `asyncio.create_task`.

- [ ] **Step 1: Write the failing tests** (substituem `test_red_tests_open_no_pr`; `test_happy_path` ganha `fake.commits[0].message == "feat: implementa ENG-12"` e `require_changes is True`)

```python
async def test_red_then_fixed_opens_pr(self) -> None:
    fake = FakeActivities(red_runs=1)
    result = await self.run_workflow(fake)
    self.assertEqual(result, ImplementResult("ENG-12", "pr_opened", PR_URL))
    self.assertEqual(fake.calls, [
        "move_issue:In Progress", "plan_exists",
        "create_sandbox", "clone_branch", "implement_plan", "run_tests", "commit_implementation", "kill_sandbox",
        "move_issue:In Progress",
        "create_sandbox", "clone_branch", "analyze_failure", "kill_sandbox",
        "create_sandbox", "clone_branch", "implement_fix", "run_tests", "commit_implementation", "kill_sandbox",
        "open_pull_request", "move_issue:In Review",
    ])
    self.assertEqual(fake.commits[0].message, "wip: tentativa 0 de ENG-12 (testes vermelhos)")
    self.assertFalse(fake.commits[0].require_changes)
    self.assertEqual(fake.commits[1].message, "feat: implementa ENG-12")
    self.assertFalse(fake.commits[1].require_changes)   # Review Focus 5: já houve checkpoint
    self.assertEqual(fake.analyze_requests[0].test_output, "FAILED test_a")
    self.assertEqual(fake.fix_requests[0].plan, "plano")

async def test_passes_previous_attempts_to_analysis(self) -> None:
    fake = FakeActivities(red_runs=5)
    await self.run_workflow(fake)
    self.assertEqual([a.number for a in fake.analyze_requests[-1].attempts], [2, 3, 4])

async def test_question_waits_for_answer_and_reanalyzes(self) -> None:
    fake = FakeActivities(red_runs=1, answers=["Use bcrypt"],
                          analyses=[FixAnalysis("causa", question="bcrypt ou argon2?"),
                                    FixAnalysis("causa", plan="plano")])
    result = await self.run_workflow(fake)
    self.assertEqual(result.outcome, "pr_opened")
    self.assertIn("bcrypt ou argon2?", fake.comments[0].body)
    self.assertIn("nesta thread", fake.comments[0].body)
    self.assertEqual(fake.analyze_requests[1].clarifications,
                     (Clarification("bcrypt ou argon2?", "Use bcrypt"),))
    self.assertEqual(fake.fix_requests[0].clarifications,
                     (Clarification("bcrypt ou argon2?", "Use bcrypt"),))
    self.assertEqual(fake.calls.count("implement_fix"), 1)  # pergunta não conta tentativa

async def test_ignores_answer_to_other_question(self) -> None:
    # signal com question_id errado e depois o certo: só o certo vale (Review Focus 2)
    ...
    self.assertEqual(fake.analyze_requests[1].clarifications[0].answer, "certa")

async def test_asks_after_max_attempts(self) -> None:
    fake = FakeActivities(red_runs=6, answers=["tente mockar o relógio"])
    result = await self.run_workflow(fake)
    self.assertEqual(result.outcome, "pr_opened")
    self.assertEqual(len(fake.comments), 1)
    self.assertIn("Fiz 5 tentativas", fake.comments[0].body)
    self.assertEqual(fake.calls.count("implement_fix"), 6)

async def test_noop_fix_keeps_looping(self) -> None:
    # Review Focus 4: a fake commit_implementation só "falha por sem mudança" se require_changes=True;
    # com red_runs=2 o loop roda duas correções sem erro
    fake = FakeActivities(red_runs=2)
    self.assertEqual((await self.run_workflow(fake)).outcome, "pr_opened")
    self.assertEqual([c.require_changes for c in fake.commits], [False, False, False])

async def test_stops_when_issue_leaves_in_progress(self) -> None:
    fake = FakeActivities(red_runs=3, stop_after_checks=1)  # 2ª checagem de status volta False
    result = await self.run_workflow(fake)
    self.assertEqual(result, ImplementResult("ENG-12", "stopped"))
    self.assertNotIn("open_pull_request", fake.calls)
    self.assertEqual(fake.calls.count("create_sandbox"), fake.calls.count("kill_sandbox"))

async def test_stops_while_waiting_for_answer(self) -> None:
    fake = FakeActivities(red_runs=1, stop_after_checks=1,
                          analyses=[FixAnalysis("causa", question="?")])  # sem resposta
    result = await self.run_workflow(fake)
    self.assertEqual(result.outcome, "stopped")

async def test_fails_without_answer_in_seven_days(self) -> None:
    fake = FakeActivities(red_runs=1, analyses=[FixAnalysis("causa", question="?")])
    with self.assertRaises(WorkflowFailureError):
        await self.run_workflow(fake)
    self.assertNotIn("open_pull_request", fake.calls)
```

`test_kills_sandbox_on_implement_failure`/`test_kills_sandbox_on_commit_failure` continuam valendo. O `run_workflow` registra também `fake.analyze_failure`, `fake.implement_fix`, `fake.post_comment` e passa `env.client` para o fake.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_implement_workflow -v`
Expected: FAIL (testes vermelhos ainda falham o workflow; `human_answer` não existe)

- [ ] **Step 3: Implement** o loop, os helpers, o signal e `format_question`/`stuck_question` em `implement_workflow.py`; importar os modelos novos dentro de `imports_passed_through()`; `import asyncio` para `asyncio.TimeoutError` do `wait_condition`. Atualizar a docstring da classe. Registrar as três activities novas em `worker.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: PASS (suíte inteira)

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/implement_workflow.py src/software_factory/orchestration/worker.py tests/test_implement_workflow.py
git commit -m "feat: loop analisar → planejar → corrigir até os testes ficarem verdes"
```

---

### Task 5: Webhook de comentário → signal `human_answer` (+ docs)

**Files:**
- Modify: `src/software_factory/linear/events.py`
- Modify: `src/software_factory/linear/webhook.py`
- Modify: `tests/payloads.py`, `tests/test_events.py`, `tests/test_webhook.py`
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `HumanAnswer` (Task 1), `ImplementIssueWorkflow.human_answer` (Task 4).
- Produces:
  - `events.py`: `@dataclass(frozen=True) class CommentReply: issue_id: str; answer: HumanAnswer` e `def parse_reply(payload: dict[str, Any]) -> CommentReply | None` — só `type == "Comment"`, `action == "create"`, com `data.parentId`, `data.issueId` e `data.body` não vazios; `answer = HumanAnswer(question_id=parentId, comment_id=data.id, body=body)`.
  - `webhook.py`: `async def handle_reply(client: Any, reply: CommentReply, delivery_id: str | None) -> bool` — `client.get_workflow_handle(f"implement-issue-{reply.issue_id}").signal(ImplementIssueWorkflow.human_answer, reply.answer)`; `RPCError` com `status == RPCStatusCode.NOT_FOUND` (sem workflow rodando) → False. No endpoint, `parse_reply` vem antes das checagens de Issue: True → `{"status": "accepted"}`; False → `{"status": "ignored"}`; outra exceção → 503.
- `tests/payloads.py`: `comment_payload(parent_id: str | None = "question-1", body: str = "Use bcrypt", action: str = "create", webhook_timestamp: int = 0)` com `data = {"id": "comment-1", "body": ..., "issueId": "issue-uuid", "parentId": ..., "userId": "user-1"}`.

- [ ] **Step 1: Write the failing tests**

```python
# test_events.py
def test_parse_reply(self) -> None:
    self.assertEqual(parse_reply(comment_payload()),
                     CommentReply("issue-uuid", HumanAnswer("question-1", "comment-1", "Use bcrypt")))
def test_parse_reply_ignores_top_level_comment(self) -> None:
    self.assertIsNone(parse_reply(comment_payload(parent_id=None)))
def test_parse_reply_ignores_update_and_issue_events(self) -> None:
    self.assertIsNone(parse_reply(comment_payload(action="update")))
    self.assertIsNone(parse_reply(issue_payload(labels=[FEATURE])))

# test_webhook.py — FakeTemporalClient ganha get_workflow_handle(id) → FakeHandle que grava
# (id, signal, arg) em self.signals e levanta self.signal_error se houver
def test_reply_signals_implement_workflow(self) -> None:
    response = self.post(json.dumps(comment_payload(webhook_timestamp=now_ms())).encode())
    self.assertEqual(response.json(), {"status": "accepted"})
    (wf_id, signal, arg), = self.temporal.signals
    self.assertEqual(wf_id, "implement-issue-issue-uuid")
    self.assertEqual(signal, ImplementIssueWorkflow.human_answer)
    self.assertEqual(arg, HumanAnswer("question-1", "comment-1", "Use bcrypt"))
def test_reply_without_running_workflow_is_ignored(self) -> None:
    # signal_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    self.assertEqual(response.status_code, 200); self.assertEqual(response.json(), {"status": "ignored"})
def test_reply_with_temporal_down_is_503(self) -> None:
    # signal_error = RuntimeError("down")
    self.assertEqual(response.status_code, 503)
def test_top_level_comment_is_ignored(self) -> None:
    self.assertEqual(response.json(), {"status": "ignored"}); self.assertEqual(self.temporal.signals, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_events tests.test_webhook -v`
Expected: FAIL com `ImportError` (`parse_reply`, `comment_payload`)

- [ ] **Step 3: Implement** `CommentReply`, `parse_reply`, `handle_reply` e o ramo no endpoint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: PASS

- [ ] **Step 5: Update docs.** `CLAUDE.md`: Stack (sandbox por **sessão**; `systematic-debugging` vendorizada junto com `writing-plans`), Estrutura (activities/modelos novos, `skills/systematic-debugging/`, `parse_reply`/`handle_reply`), seção do webhook (Comment reply → signal; recurso Comments no webhook) e o parágrafo do `ImplementIssueWorkflow` (loop, checkpoints `wip:` em `factory/<ID>`, PR só com verde, `MAX_FIX_ATTEMPTS`, perguntas na thread, parar movendo a issue, 7 dias sem resposta = falha). `README.md`: o mesmo fluxo em resumo e o passo de habilitar Comments no webhook do Linear.

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/linear tests/payloads.py tests/test_events.py tests/test_webhook.py CLAUDE.md README.md
git commit -m "feat: reply no Linear responde a pergunta do loop de correção"
```
