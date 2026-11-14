# Implementação do plano e abertura de PR — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando uma issue entra em `In Progress`, um novo `ImplementIssueWorkflow` pega o plano commitado em `factory/<IDENTIFIER>`, implementa-o com o Claude Code num sandbox E2B, roda os testes, e só se estiverem verdes faz push, abre a PR e move a issue para `In Review`. Sem plano, a issue volta para `In Planning`.

**Architecture:** O gatilho é o webhook do Linear (`update` com `updatedFrom.stateId` e `data.state.name == "In Progress"`), o que cobre tanto o `PlanIssueWorkflow` movendo a issue quanto uma pessoa movendo à mão. O workflow novo (id `implement-issue-<issue.id>`) valida o status, checa o plano via API REST do GitHub (antes de gastar um sandbox), cria o sandbox, clona a branch do plano, roda o Claude com `Bash` liberado, verifica os testes com um comando próprio (`TEST_COMMAND`, sem confiar no relato do Claude), commita/pusha pela activity, destrói o sandbox no `finally`, abre a PR pela API do GitHub e move a issue para `In Review`.

**Tech Stack:** Python 3.12, temporalio, e2b (`AsyncSandbox`), httpx (API REST do GitHub e GraphQL do Linear), FastAPI, unittest.

**Spec:** pedido do usuário em 2026-11-12: "After the issue is moved to the status In Progress, you need to get the generated plan and implement it. If there is no plan, move the issue back to the In Planning status. Do as you did for the planning part, create the sandbox and so on. Implement the plan, commit and open the PR if all tests go green. When all tests pass and the PR is opened, move the issue to the status In Review."

## Global Constraints

- Nenhuma dependência nova: GitHub REST via `httpx` puro (`https://api.github.com`), como o `LinearClient`.
- Headers do GitHub: `Authorization: Bearer <GITHUB_TOKEN>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.
- `GITHUB_TOKEN` passa a precisar de **Contents: Read and write** e **Pull requests: Read and write** no repo alvo.
- Nova env obrigatória do worker: `TEST_COMMAND` (ex.: `uv pip install --system -e ".[dev]" && python -m pytest`), executada em `REPO_DIR` dentro do sandbox. Exit code 0 = verde.
- Status (em `orchestration/models.py`): reutiliza `STATE_IN_PLANNING`, `STATE_IN_PROGRESS`; novo `STATE_IN_REVIEW = "In Review"`.
- Branch e caminho do plano: os mesmos de hoje, `plan_branch(identifier)` = `factory/<ID>` e `plan_path(identifier)` = `docs/superpowers/plans/<ID>.md`. A PR é `factory/<ID>` → branch default do repo e contém o commit do plano + os da implementação.
- Um sandbox por execução de workflow, sempre destruído no `finally`; nada de push nem PR com testes vermelhos.
- Orçamento do sandbox (55 min de vida): comando do Claude `IMPLEMENT_COMMAND_TIMEOUT_S = 30 * 60`, testes `TEST_COMMAND_TIMEOUT_S = 10 * 60`; `implement_plan` com `maximum_attempts=1` (um retry estouraria a vida do sandbox).
- O token nunca fica no `origin` do repo clonado para implementação (o Claude tem `Bash`): após o clone, `git remote set-url origin https://github.com/<repo>.git`; `ls-remote`/`push` usam a URL com `$GITHUB_TOKEN` via `envs=`, nunca o token literal no comando.
- Webhook: issue sem label `Feature`/`Bug` não dispara implementação; evento que adiciona label **e** muda status dispara só o planejamento.
- Sem compensação em falha: a issue fica em `In Progress`.
- Logs, mensagens e docs em português.

## Review Focus

1. `GITHUB_TOKEN` errado ou sem acesso ao repo (GitHub responde 404 em repo privado sem permissão) — não pode ser lido como "sem plano" e devolver a issue para `In Planning`; `plan_exists` primeiro consulta o repo e falha não-retentável. Teste em Task 2.
2. Retry de `open_pull_request` depois de a PR já ter sido criada (ou issue que volta a `In Progress` com PR aberta) — retorna a URL da PR existente, sem `POST`. Teste em Task 2.
3. Retry de `commit_implementation` depois de um push que já aconteceu — remoto == local, sem novo push. Teste em Task 3.
4. Claude não mudou nada (HEAD == commit clonado) — falha não-retentável, nada de push nem PR. Testes em Task 3 e Task 4.
5. Token visível para o Claude via `.git/config` — o `origin` é reescrito sem token antes de o Claude rodar. Teste em Task 3.

---

### Task 1: `GitHubClient` REST

**Files:**
- Create: `src/software_factory/github/__init__.py` (vazio), `src/software_factory/github/client.py`
- Test: `tests/test_github_client.py`

**Interfaces:**
- Produces:
  - `GITHUB_API_URL = "https://api.github.com"`
  - `class GitHubClient(token: str, repo: str, transport: httpx.AsyncBaseTransport | None = None)` — `repo` é `owner/repo`.
  - `async default_branch() -> str` — `GET /repos/{repo}` → `default_branch`.
  - `async get_file(path: str, ref: str) -> str | None` — `GET /repos/{repo}/contents/{path}?ref={ref}` com `Accept: application/vnd.github.raw+json`; texto do arquivo, ou `None` em 404 (branch ou arquivo inexistente).
  - `async find_open_pull(head: str) -> str | None` — `GET /repos/{repo}/pulls?head={owner}:{head}&state=open`; `html_url` da primeira ou `None`.
  - `async create_pull(title: str, head: str, base: str, body: str) -> str` — `POST /repos/{repo}/pulls`; retorna `html_url`.

Regras de erro (fora o 404 de `get_file`): 429, ≥ 500, ou 403 com header `x-ratelimit-remaining: 0` → `ApplicationError` retentável; qualquer outro não-2xx → não-retentável, mensagem com status e `message` do corpo. `httpx.TransportError` propaga.

- [ ] **Step 1: Escrever os testes** com `httpx.MockTransport` gravando as requests, no estilo de `tests/test_linear_client.py`:
  - `test_default_branch`: resposta `{"default_branch": "main"}` → `"main"`; request em `/repos/acme/app` com `Authorization: Bearer gh-token` e `X-GitHub-Api-Version: 2022-11-28`.
  - `test_get_file_returns_text`: 200 com corpo `"# Plano"` → `"# Plano"`; query `ref=factory/ENG-12`; `Accept: application/vnd.github.raw+json`.
  - `test_get_file_missing_returns_none`: 404 → `None`.
  - `test_find_open_pull`: `[{"html_url": "https://github.com/acme/app/pull/7"}]` → essa URL; query `head=acme:factory/ENG-12`, `state=open`. Lista vazia → `None`.
  - `test_create_pull`: 201 `{"html_url": ".../pull/8"}` → URL; corpo JSON `{"title", "head", "base", "body"}`.
  - `test_rate_limited_is_retryable`: 429 e 403 com `x-ratelimit-remaining: 0` → `ApplicationError` com `non_retryable is False`.
  - `test_server_error_is_retryable`: 502 → retentável.
  - `test_auth_error_is_not_retryable`: 401 em `default_branch` e 404 em `default_branch` → `non_retryable is True`.
  - `test_unprocessable_is_not_retryable`: 422 em `create_pull` → não-retentável, mensagem contém o `message` do corpo.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_client -v`
Expected: FAIL com `ModuleNotFoundError: software_factory.github`

- [ ] **Step 3: Implementar `GitHubClient`** em `src/software_factory/github/client.py`, com um `_request(method, path, **kwargs) -> httpx.Response` privado que aplica as regras de erro; `get_file` checa 404 antes de delegar o tratamento.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_client -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/github tests/test_github_client.py
git commit -m "feat: GitHubClient REST para plano e pull requests"
```

---

### Task 2: `GitHubActivities` — `plan_exists` e `open_pull_request`

**Files:**
- Modify: `src/software_factory/orchestration/models.py`
- Create: `src/software_factory/orchestration/github_activities.py`
- Test: `tests/test_github_activities.py`

**Interfaces:**
- Consumes: `GitHubClient` (Task 1); `plan_path`, `plan_branch` de `orchestration/activities.py`.
- Produces (em `models.py`):
  - `STATE_IN_REVIEW = "In Review"`
  - `@dataclass(frozen=True) class PullRequestRequest: identifier: str; title: str; url: str | None`
- Produces (em `github_activities.py`):
  - `class GitHubActivities(client: GitHubClient)`
  - `@activity.defn async plan_exists(identifier: str) -> bool`
  - `@activity.defn async open_pull_request(request: PullRequestRequest) -> str` — URL da PR.

Comportamento:
- `plan_exists`: chama `default_branch()` primeiro (valida acesso ao repo; erro de auth/404 propaga não-retentável), depois `get_file(plan_path(id), plan_branch(id))`. `False` se `None` ou só espaços.
- `open_pull_request`: `find_open_pull(plan_branch(id))`; se achar, retorna sem criar. Senão `create_pull(title=f"{identifier}: {title}", head=plan_branch(id), base=default_branch(), body=...)`. Corpo fixo:

```
Implementa {identifier} ({url or "-"}).

Plano: `{plan_path(identifier)}`

🤖 Gerado pela software factory com [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 1: Escrever os testes** com um `FakeGitHubClient` (métodos async que gravam chamadas e devolvem valores configurados), rodando via `ActivityEnvironment`:
  - `test_plan_exists`: `get_file` devolve `"# Plano"` → `True`; chamada com `("docs/superpowers/plans/ENG-12.md", "factory/ENG-12")`; `default_branch` chamado antes.
  - `test_plan_missing`: `get_file` → `None` → `False`; e `"  \n"` → `False`.
  - `test_plan_exists_propagates_repo_access_error`: `default_branch` levanta `ApplicationError(non_retryable=True)` → a activity levanta o mesmo e `get_file` não é chamado.
  - `test_open_pull_request_creates`: `find_open_pull` → `None`, `default_branch` → `"main"` → retorna a URL de `create_pull`; argumentos `title="ENG-12: Adicionar login"`, `head="factory/ENG-12"`, `base="main"`, body contém a URL do Linear e `docs/superpowers/plans/ENG-12.md`.
  - `test_open_pull_request_reuses_existing`: `find_open_pull` → `".../pull/7"` → retorna essa URL e `create_pull` não é chamado.
  - `test_plan_exists_rejects_bad_identifier`: `"ENG-12; rm"` → `ApplicationError` não-retentável, sem chamadas ao cliente.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_activities -v`
Expected: FAIL com `ModuleNotFoundError`

- [ ] **Step 3: Adicionar `STATE_IN_REVIEW` e `PullRequestRequest` em `models.py` e implementar `GitHubActivities`** em `github_activities.py`. Validar o identifier (`plan_path`) antes de qualquer chamada.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_activities -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/models.py src/software_factory/orchestration/github_activities.py tests/test_github_activities.py
git commit -m "feat: activities plan_exists e open_pull_request"
```

---

### Task 3: Activities de sandbox para implementar, testar e commitar

**Files:**
- Modify: `src/software_factory/config.py` (`test_command`), `src/software_factory/orchestration/models.py`, `src/software_factory/orchestration/activities.py`, `.env.example`
- Test: `tests/test_activities.py`

**Interfaces:**
- Consumes: `PlanIssueInput` (já existe; serve de entrada da issue também aqui).
- Produces (em `models.py`):
  - `CloneBranchRequest(sandbox_id: str, identifier: str)`
  - `ImplementRequest(sandbox_id: str, issue: PlanIssueInput)`
  - `TestRunResult(passed: bool, output: str)`
  - `CommitImplementationRequest(sandbox_id: str, identifier: str, base_sha: str)`
- Produces (em `config.py`): `FactorySettings.test_command: str`, lido de `TEST_COMMAND` (obrigatória em `from_env`). Como o dataclass tem campos com default, declarar `test_command` logo depois de `linear_api_key`, sem default; atualizar o `SETTINGS` dos testes com `test_command="python -m pytest"`.
- Produces (em `activities.py`, métodos de `SandboxActivities`):
  - `clone_branch(request: CloneBranchRequest) -> str` — SHA do commit clonado (`refs/remotes/origin/factory/<ID>`).
  - `implement_plan(request: ImplementRequest) -> str` — texto final do Claude.
  - `run_tests(sandbox_id: str) -> TestRunResult`
  - `commit_implementation(request: CommitImplementationRequest) -> str` — nome da branch.
  - funções `build_implement_prompt(issue: PlanIssueInput, test_command: str) -> str` e `implement_command() -> str`.
  - Renomear `parse_plan` → `parse_result` (genérico; usado por `generate_plan` e `implement_plan`).
  - Extrair o heartbeat de `generate_plan` para `async _run_with_heartbeat(sandbox, cmd: str, timeout: int) -> Any`, usado pelas três execuções longas (plano, implementação, testes).

Constantes novas: `IMPLEMENT_PROMPT_PATH = "/home/user/implement-prompt.md"`, `IMPLEMENT_COMMAND_TIMEOUT_S = 30 * 60`, `TEST_COMMAND_TIMEOUT_S = 10 * 60`, `TEST_OUTPUT_TAIL = 4000`.

Comportamento:
- `clone_branch`: se `REPO_DIR/.git` não existe, `git clone --depth 1 --branch factory/<ID> "https://x-access-token:$GITHUB_TOKEN@github.com/<repo>.git" REPO_DIR` com `envs={"GITHUB_TOKEN": ...}`. Sempre (também em retry): `git remote set-url origin https://github.com/<repo>.git`, `git config user.name "software-factory"`, `git config user.email "software-factory@users.noreply.github.com"`, e retorna `git rev-parse refs/remotes/origin/factory/<ID>`.
- `implement_command()`: `cat IMPLEMENT_PROMPT_PATH | claude -p --output-format stream-json --verbose --allowedTools "Read,Grep,Glob,Edit,Write,Bash"`.
- `build_implement_prompt`: pede para implementar todas as tarefas do plano em `plan_path(id)`, seguindo os passos (TDD); rodar `test_command` até ficar verde; commitar localmente por tarefa; **não** fazer push nem mexer em remotes; pode haver trabalho parcial de tentativa anterior (ver `git log`/`git status`); não fazer perguntas; terminar com um resumo. Inclui identifier, título, URL e descrição da issue (`"(sem descrição)"` se `None`).
- `implement_plan`: escreve o prompt, roda com `_run_with_heartbeat(..., IMPLEMENT_COMMAND_TIMEOUT_S)` em `cwd=REPO_DIR`, retorna `parse_result(stdout)`.
- `run_tests`: roda `settings.test_command` em `REPO_DIR` com `TEST_COMMAND_TIMEOUT_S`; `CommandExitException` (de `e2b`) → `TestRunResult(False, tail)`; sucesso → `TestRunResult(True, tail)`. `tail` = últimos `TEST_OUTPUT_TAIL` caracteres de `stdout + stderr`.
- `commit_implementation`: `git add -A`; `git diff --cached --quiet || git commit -m "feat: implementa <ID>"`; `local = git rev-parse HEAD`; se `local == base_sha` → `ApplicationError("Claude Code não fez mudanças", non_retryable=True)`. `remote` = primeiro campo de `git ls-remote --heads "<url com $GITHUB_TOKEN>" refs/heads/factory/<ID>` (com `envs`). `remote == local` → retorna a branch; `remote != base_sha` → não-retentável "branch ... mudou no remoto"; senão `git push "<url com $GITHUB_TOKEN>" HEAD:refs/heads/factory/<ID>` (com `envs`) e retorna a branch.

- [ ] **Step 1: Escrever os testes** em `tests/test_activities.py`, reaproveitando `FakeSandbox`/`FakeCommands` (estender `FakeCommands` com `fail: dict[str, tuple[str, str, int]]` — substring do comando → `(stdout, stderr, exit_code)` que levanta `CommandExitException(stderr=..., stdout=..., exit_code=..., error=None)`):
  - `test_from_env_requires_test_command`: sem `TEST_COMMAND` → `RuntimeError`; com → `settings.test_command`.
  - `test_clone_branch`: comandos na ordem clone (`--branch factory/ENG-12`, `envs={"GITHUB_TOKEN": "gh-token"}`, sem `gh-token` no texto) → `git remote set-url origin https://github.com/acme/app.git` → os dois `git config` → `git rev-parse refs/remotes/origin/factory/ENG-12`; retorna o SHA configurado em `responses`.
  - `test_clone_branch_retry_skips_clone_but_resets_origin`: com `.git` existente, não há `git clone`, mas há `set-url` e `rev-parse`.
  - `test_implement_prompt`: contém `ENG-12`, título, `docs/superpowers/plans/ENG-12.md`, o test command, e as palavras `push` (proibição); descrição `None` → `"sem descrição"`.
  - `test_implement_plan`: prompt escrito em `/home/user/implement-prompt.md`; comando contém `claude -p`, `--output-format stream-json` e `Bash`; `cwd == REPO_DIR`; houve heartbeat; retorna o `result` do stream.
  - `test_implement_plan_error_result`: `subtype: error_max_turns` → `ApplicationError`.
  - `test_run_tests_green`: `TestRunResult(True, ...)`; comando == `"python -m pytest"`, `cwd == REPO_DIR`.
  - `test_run_tests_red`: `fail={"pytest": ("x" * 5000, "FAILED test_a", 1)}` → `passed is False`, `len(output) == 4000`, `output.endswith("FAILED test_a")`.
  - `test_commit_implementation_pushes`: `responses={"rev-parse": "def456\n", "ls-remote": "abc123\trefs/heads/factory/ENG-12\n"}`, `base_sha="abc123"` → retorna `"factory/ENG-12"`; último comando é `git push "https://x-access-token:$GITHUB_TOKEN@github.com/acme/app.git" HEAD:refs/heads/factory/ENG-12` com `envs={"GITHUB_TOKEN": "gh-token"}`; nenhum comando contém `gh-token`.
  - `test_commit_implementation_idempotent_after_push`: `ls-remote` devolve `def456` → sem `git push`.
  - `test_commit_implementation_rejects_no_changes`: `rev-parse` == `base_sha` → não-retentável, sem `ls-remote` nem push.
  - `test_commit_implementation_rejects_moved_remote`: `ls-remote` devolve `zzz999` → não-retentável, sem push.
  - Os testes existentes de `generate_plan` continuam passando sem mudança além do `SETTINGS`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: FAIL (`ImportError`/`AttributeError` para os nomes novos)

- [ ] **Step 3: Implementar** os modelos, `test_command` em `FactorySettings`, `_run_with_heartbeat`, `parse_result` e as quatro activities em `activities.py`. Adicionar em `.env.example`, abaixo de `TARGET_REPO`:

```
# Comando que roda os testes do repo alvo no sandbox (exit 0 = verde); a PR só abre com verde
TEST_COMMAND=
```

e atualizar o comentário de `GITHUB_TOKEN` para "Contents: Read and write e Pull requests: Read and write".

- [ ] **Step 4: Rodar a suíte toda**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/config.py src/software_factory/orchestration/models.py src/software_factory/orchestration/activities.py .env.example tests/test_activities.py
git commit -m "feat: activities de sandbox para implementar, testar e commitar"
```

---

### Task 4: `ImplementIssueWorkflow` e registro no worker

**Files:**
- Modify: `src/software_factory/orchestration/models.py`
- Create: `src/software_factory/orchestration/implement_workflow.py`
- Modify: `src/software_factory/orchestration/worker.py`
- Test: `tests/test_implement_workflow.py`

**Interfaces:**
- Consumes: activities por nome — `move_issue` (`MoveIssueRequest`), `plan_exists`, `open_pull_request` (Task 2), `create_sandbox`, `clone_branch`, `implement_plan`, `run_tests`, `commit_implementation` (Task 3), `kill_sandbox`; `MOVE_ISSUE_TIMEOUT`, `MOVE_ISSUE_RETRY` de `workflow.py`.
- Produces (em `models.py`):
  - `OUTCOME_PR_OPENED = "pr_opened"`, `OUTCOME_NO_PLAN = "no_plan"`, `OUTCOME_SKIPPED = "skipped"`
  - `ImplementResult(identifier: str, outcome: str, pr_url: str | None = None)`
- Produces: `@workflow.defn class ImplementIssueWorkflow` com `run(issue: PlanIssueInput) -> ImplementResult`.

Fluxo (timeouts/retries):
1. `move_issue(MoveIssueRequest(id, to_state=In Progress, from_state=In Progress, require_states=(In Review, In Planning)))` — valida os status cedo sem mover nada; `False` (issue saiu de In Progress) → `ImplementResult(id, OUTCOME_SKIPPED)`.
2. `plan_exists(identifier)` (30 s, 5 tentativas). `False` → `move_issue(to_state=In Planning, from_state=In Progress)` e `ImplementResult(id, OUTCOME_NO_PLAN)`; nenhum sandbox criado.
3. `create_sandbox(issue_id)` (3 min, 3 tentativas).
4. `try`: `clone_branch` (5 min, 3) → `implement_plan` (35 min, heartbeat 2 min, `maximum_attempts=1`) → `run_tests` (12 min, heartbeat 2 min, 1 tentativa); se `not passed` → `raise ApplicationError(f"testes falharam:\n{output}", non_retryable=True)` → `commit_implementation` (3 min, 3). `finally`: `kill_sandbox` (1 min, 5).
5. `open_pull_request(PullRequestRequest(identifier, title, url))` (30 s, 5).
6. `move_issue(to_state=In Review)`; retorna `ImplementResult(id, OUTCOME_PR_OPENED, pr_url)`.

`run_tests` também precisa de heartbeat: usa `_run_with_heartbeat` (Task 3).

- [ ] **Step 1: Escrever os testes** em `tests/test_implement_workflow.py`, no molde de `tests/test_workflow.py` (`FakeActivities` com flags `not_in_progress`, `no_plan`, `tests_red`, `fail_implement`, `fail_commit`; `calls` com `move_issue:<to_state>`):
  - `test_happy_path`: resultado `ImplementResult("ENG-12", "pr_opened", "https://github.com/acme/app/pull/7")`; `calls == ["move_issue:In Progress", "plan_exists", "create_sandbox", "clone_branch", "implement_plan", "run_tests", "commit_implementation", "kill_sandbox", "open_pull_request", "move_issue:In Review"]`; `moves[0] == MoveIssueRequest("issue-1", to_state="In Progress", from_state="In Progress", require_states=("In Review", "In Planning"))`; `CommitImplementationRequest.base_sha` igual ao retornado por `clone_branch`.
  - `test_skips_when_not_in_progress`: `calls == ["move_issue:In Progress"]`, outcome `skipped`.
  - `test_no_plan_moves_back_to_planning`: `calls == ["move_issue:In Progress", "plan_exists", "move_issue:In Planning"]`, outcome `no_plan`, último move com `from_state="In Progress"`.
  - `test_red_tests_open_no_pr`: `WorkflowFailureError`; `calls[-1] == "kill_sandbox"`; sem `commit_implementation`, `open_pull_request` nem `move_issue:In Review`.
  - `test_kills_sandbox_on_implement_failure` e `test_kills_sandbox_on_commit_failure`: `WorkflowFailureError`, `calls[-1] == "kill_sandbox"`, sem `open_pull_request`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_implement_workflow -v`
Expected: FAIL com `ModuleNotFoundError: ...implement_workflow`

- [ ] **Step 3: Implementar** os modelos de resultado e `ImplementIssueWorkflow` (imports de `models` dentro de `workflow.unsafe.imports_passed_through()`). Em `worker.py`: registrar `ImplementIssueWorkflow` em `workflows` e `clone_branch`, `implement_plan`, `run_tests`, `commit_implementation`, `github.plan_exists`, `github.open_pull_request` em `activities`, com `github = GitHubActivities(GitHubClient(settings.github_token, settings.target_repo))`.

- [ ] **Step 4: Rodar a suíte toda**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/models.py src/software_factory/orchestration/implement_workflow.py src/software_factory/orchestration/worker.py tests/test_implement_workflow.py
git commit -m "feat: ImplementIssueWorkflow implementa o plano e abre a PR"
```

---

### Task 5: Gatilho no webhook e documentação

**Files:**
- Modify: `src/software_factory/linear/events.py`, `src/software_factory/linear/webhook.py`, `tests/payloads.py`
- Modify: `README.md`, `CLAUDE.md`
- Test: `tests/test_events.py`, `tests/test_webhook.py`

**Interfaces:**
- Consumes: `ImplementIssueWorkflow.run` (Task 4), `STATE_IN_PROGRESS`.
- Produces:
  - `events.should_implement(payload: dict[str, Any], trigger_labels: Iterable[str]) -> bool` — `type == "Issue"`, `action == "update"`, `"stateId" in updatedFrom`, `data.state.name` igual a `In Progress` (case-insensitive) e a issue tem label de gatilho.
  - `webhook.handle_issue(client, task_queue, issue, delivery_id, workflow_run=PlanIssueWorkflow.run, id_prefix="plan-issue") -> bool` — id `f"{id_prefix}-{issue.id}"`; o resto igual.
  - `tests/payloads.issue_payload(..., state: dict[str, str] | None = None)` — quando dado, vai em `data["state"]`.

Roteamento em `linear_webhook`: `should_process` → planejamento (como hoje); senão `should_implement` → `handle_issue(..., workflow_run=ImplementIssueWorkflow.run, id_prefix="implement-issue")`; senão `ignored`.

- [ ] **Step 1: Escrever os testes**
  - `tests/test_events.py`: `should_implement` `True` para `update` com `updatedFrom={"stateId": "s-old"}`, `state={"id": "s1", "name": "In Progress"}` e label `FEATURE`; `False` sem label de gatilho, com `state.name == "In Review"`, sem `stateId` em `updatedFrom`, e em `create`. Label adicionada + estado novo `In Progress` no mesmo evento: `should_process` é `True` (o planejamento vence no roteamento).
  - `tests/test_webhook.py`: `test_in_progress_starts_implement_workflow` — id `implement-issue-issue-uuid`, `args[0] is ImplementIssueWorkflow.run`; `test_label_added_and_in_progress_starts_plan` — id começa com `plan-issue-`; `test_in_progress_without_label_is_ignored` → `{"status": "ignored"}`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_events tests.test_webhook -v`
Expected: FAIL (`should_implement` não existe)

- [ ] **Step 3: Implementar** `should_implement`, o parâmetro `state` do fixture e o roteamento no webhook.

- [ ] **Step 4: Atualizar a documentação**
  - `README.md`: "Hoje" passa a cobrir as quatro etapas; o mermaid ganha o ramo do `ImplementIssueWorkflow` (webhook `In Progress` → status ainda In Progress? → plano existe? não → In Planning; sim → sandbox → clona `factory/<ID>` → Claude implementa → testes verdes? não → falha, sandbox destruído; sim → push → destrói sandbox → abre PR → In Review), sem as linhas pontilhadas de "próximos passos"; tabela de env com `TEST_COMMAND` e as permissões novas do `GITHUB_TOKEN`.
  - `CLAUDE.md`: Stack (`GITHUB_TOKEN` com Pull requests RW; GitHub REST via httpx em `github/client.py`; `TEST_COMMAND`), Estrutura (`github/`, `github_activities.py`, `implement_workflow.py`, activities novas), seção do webhook (gatilho de implementação, id `implement-issue-<id>`, status, sem compensação; remover "Implementação e abertura de PR ainda fora do escopo") e a lista de envs do comando do worker.

- [ ] **Step 5: Rodar a suíte toda**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/linear tests/payloads.py tests/test_events.py tests/test_webhook.py README.md CLAUDE.md
git commit -m "feat: webhook inicia ImplementIssueWorkflow quando a issue entra em In Progress"
```
