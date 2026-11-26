# Repo por projeto do Linear — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fábrica descobre o repo alvo de cada issue pelo projeto do Linear em que ela está, lê o comando de testes de um `.factory.toml` no próprio repo e autentica no GitHub com um GitHub App que emite um token por repo, com validade de 1h. Saem `TARGET_REPO`, `TEST_COMMAND` e `GITHUB_TOKEN`.

**Architecture:** Uma activity `resolve_repo` resolve a issue para um `RepoTarget(repo, test_command)`. A ordem é: (1) anexo da issue que aponta para `https://github.com/<repo>/tree/factory/...` (o "pin"), (2) o único link de repo do GitHub nos Resources do projeto, (3) `.factory.toml` lido da branch default via API. Cada workflow resolve uma vez no início e passa o `RepoTarget` em todos os requests de activity que tocam o repo. Se não resolver, o workflow comenta na issue e falha sem retry. O `PlanIssueWorkflow` grava o pin depois do commit do plano, e implementação e review usam o mesmo repo mesmo que o projeto mude depois. As activities pedem o token a um `TokenProvider` (repo → token). No fim, quem emite o token é o `GitHubApp`, com o token restrito ao repo da issue.

**Tech Stack:** Python 3.12, temporalio, httpx, `tomllib` (stdlib), Linear GraphQL (`attachmentCreate`), GitHub REST (App installation tokens), **PyJWT[crypto]** (nova), unittest.

**Spec:** recomendação desta conversa (2026-11-25): o Linear guarda o vínculo projeto → repo, o repo guarda como trabalhar nele (`.factory.toml`), sem banco sincronizado por webhook. Respostas do usuário: GitHub App **neste plano**; issue sem repo válido **sempre falha com comentário**, sem fallback para `TARGET_REPO`.

## Decisões tomadas neste plano

- **Link do projeto:** conta toda URL `http(s)://(www.)github.com/<owner>/<repo>[/...]` dos Resources (external links) do projeto. Dedup sem diferenciar maiúsculas. Tem de haver exatamente um repo distinto. Zero ou mais de um vira problema.
- **Pin:** anexo da issue com URL `https://github.com/<owner>/<repo>/tree/factory/<qualquer coisa>`. O pin vence o projeto. Dois pins de repos diferentes viram problema. Para trocar o repo de uma issue já planejada, a pessoa apaga o anexo. O pin é gravado com melhor esforço: se falhar, só loga warning, e a resolução cai no projeto.
- **`.factory.toml`:** lido da **branch default** (nunca de `factory/<ID>`, onde o Claude tem `Bash`). Chave obrigatória: `test_command` (string não vazia). Entra em `SENSITIVE_PATTERNS`.
- **Problema de resolução:** a activity devolve `RepoResolution(problem=...)` em vez de levantar erro. O workflow posta o comentário e levanta `ApplicationError(non_retryable=True)`. O 4xx do GitHub (sem acesso, App não instalado) também vira problema. Já 5xx e rate limit propagam e o Temporal retenta.
- **Onde resolve:** no plano, **antes** de mover para `In Planning` (a issue continua em `To Do`). Na implementação e na review, logo depois da checagem de status e antes de qualquer outra coisa.
- **Token:** `TokenProvider = Callable[[str], Awaitable[str]]`. O `clone_repo` passa a tirar o token do `origin`, como o `clone_branch` já faz, e o `commit_plan` passa a fazer o push com um token novo via `_authed_url`. Com tokens de 1h, um plano de 45 min não pode depender do token do clone.
- **GitHub App:** JWT RS256 (`iat = agora − 60 s`, `exp = agora + 540 s`, `iss = app_id`). `GET /repos/{repo}/installation` e depois `POST /app/installations/{id}/access_tokens` com `repositories: [<nome>]` e `permissions: {contents: write, pull_requests: write}`. Cache por repo, renovado quando faltam menos de 10 min para expirar. A instalação do App é a allowlist de repos.

## Global Constraints

- Única dependência nova: `pyjwt[crypto]>=2.8` (aprovada pelo usuário em 2026-11-25).
- Workflows determinísticos: Linear e GitHub só em activities. `pin_url` (pura) pode rodar no workflow.
- **Deploy:** a Task 5 muda assinaturas de activity e a sequência de comandos dos workflows. Não pode haver `plan-issue-*`, `implement-issue-*` ou `review-issue-*` rodando no deploy: espere terminarem ou termine-os.
- Envs removidas: `TARGET_REPO` e `TEST_COMMAND` (Task 5), `GITHUB_TOKEN` (Task 7). Envs novas: `GITHUB_APP_ID` e `GITHUB_APP_PRIVATE_KEY_PATH` (Task 7).
- Arquivo de config: `.factory.toml` na raiz do repo alvo, chave `test_command`.
- Mensagens (problemas, comentários, logs) em pt-BR, como o resto do código.
- Testes: `PYTHONPATH=src python3 -m unittest discover -s tests` (tudo verde ao fim de cada task).
- Base: `feat/token-usage-report` (ou `main`, depois do merge dela). Branch de trabalho: `feat/repo-per-project`.

## Review Focus

- Variantes do link do projeto: `https://github.com/acme/app.git`, `.../app/`, `.../app/tree/main`, `https://www.github.com/acme/app` resolvem para `acme/app`. `https://github.com/acme` e `https://github.com/orgs/acme/projects/1` não contam como repo (Task 2).
- Mesmo repo linkado duas vezes (`/acme/app` e `/Acme/App/issues`) é **um** repo, não "mais de um" (Task 2 e Task 3).
- Link com caracteres de shell (`https://github.com/acme/app;rm`) não vira repo. O nome do repo vai para comandos `git` no sandbox (Task 2).
- `.factory.toml` vem da branch default, e não de `factory/<ID>` (Task 3).
- Sessão de plano longa (~45 min) seguida de push: o `commit_plan` pede um token novo e não usa o do clone (Task 4).

---

### Task 1: `GitHubApp` — token de instalação por repo

**Files:**
- Create: `src/software_factory/github/app.py`
- Modify: `pyproject.toml` (dependência)
- Test: `tests/test_github_app.py`

**Interfaces:**
- Consumes: `GitHubClient._check(response)` (staticmethod de `github/client.py`) para classificar os erros (5xx/429/rate limit retentáveis; o resto não).
- Produces:
  - `TOKEN_REFRESH_MARGIN = timedelta(minutes=10)`
  - `INSTALLATION_PERMISSIONS = {"contents": "write", "pull_requests": "write"}`
  - `class GitHubApp(app_id: str, private_key: str, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], datetime] = <agora UTC>)`
  - `GitHubApp.app_jwt() -> str`
  - `async GitHubApp.installation_token(repo: str) -> str` (repo = `owner/name`)

- [ ] **Step 1: Adicionar a dependência e instalar**

Em `pyproject.toml`, `dependencies` ganha `"pyjwt[crypto]>=2.8"`. Rode `uv pip install -e ".[dev]"`.

- [ ] **Step 2: Escrever os testes que falham**

Gere a chave RSA no `setUpClass` com `cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key(65537, 2048)`, serializada em PEM. Use `httpx.MockTransport` como em `tests/test_github_client.py` e um relógio fixo e mutável.

```python
def test_app_jwt_claims(self):
    claims = jwt.decode(app.app_jwt(), public_pem, algorithms=["RS256"])
    self.assertEqual(claims["iss"], "123")
    self.assertEqual(claims["iat"], int(NOW.timestamp()) - 60)
    self.assertEqual(claims["exp"], int(NOW.timestamp()) + 540)

async def test_installation_token_scoped_to_repo(self):
    # GET /repos/acme/app/installation → {"id": 9}; POST /app/installations/9/access_tokens → 201
    token = await app.installation_token("acme/app")
    self.assertEqual(token, "ghs_1")
    self.assertEqual(post_body, {"repositories": ["app"], "permissions": INSTALLATION_PERMISSIONS})
    self.assertTrue(post_headers["authorization"].startswith("Bearer ey"))

async def test_token_cached_until_refresh_margin(self):
    # expires_at = NOW + 1h
    await app.installation_token("acme/app"); await app.installation_token("acme/app")
    self.assertEqual(token_posts, 1)
    clock.now = NOW + timedelta(minutes=51)   # faltam 9 min < margem
    await app.installation_token("acme/app")
    self.assertEqual(token_posts, 2)

async def test_cache_is_per_repo(self):  # acme/app e acme/api → 2 POSTs
async def test_not_installed_is_non_retryable(self):
    # GET installation → 404
    with self.assertRaises(ApplicationError) as ctx: await app.installation_token("acme/app")
    self.assertTrue(ctx.exception.non_retryable)
    self.assertIn("acme/app", str(ctx.exception))
async def test_5xx_is_retryable(self):  # POST → 502 → ApplicationError com non_retryable False
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_app -v`
Expected: FAIL (`ModuleNotFoundError: software_factory.github.app`)

- [ ] **Step 4: Implementar `GitHubApp` em `src/software_factory/github/app.py`**

O JWT sai de `jwt.encode({...}, private_key, algorithm="RS256")`. As requisições usam `httpx.AsyncClient(base_url=GITHUB_API_URL, transport=..., timeout=REQUEST_TIMEOUT_S)` com os mesmos headers `Accept` e `X-GitHub-Api-Version` do `GitHubClient`. O 404 da instalação vira `ApplicationError(f"GitHub App não está instalado em {repo}", non_retryable=True)`. O `expires_at` do GitHub (ISO com `Z`) é parseado com `datetime.fromisoformat`. O cache é um `dict[str, tuple[str, datetime]]` com chave `repo.lower()`.

- [ ] **Step 5: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_github_app -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/software_factory/github/app.py tests/test_github_app.py
git commit -m "feat: GitHubApp emite token de instalação por repo"
```

---

### Task 2: Regras puras — link do repo, pin e `.factory.toml`

**Files:**
- Create: `src/software_factory/orchestration/repos.py`
- Test: `tests/test_repos.py`

**Interfaces:**
- Produces:
  - `FACTORY_CONFIG_PATH = ".factory.toml"`
  - `repo_from_url(url: str) -> str | None`: `"owner/name"`, ou None se não for repo do GitHub
  - `linked_repos(urls: Iterable[str]) -> list[str]`: repos distintos (sem diferenciar maiúsculas), na ordem em que aparecem, com a grafia da primeira ocorrência
  - `pinned_repos(urls: Iterable[str]) -> list[str]`: o mesmo, mas só para URLs `/<owner>/<repo>/tree/factory/<...>`
  - `pin_url(repo: str, identifier: str) -> str` → `f"https://github.com/{repo}/tree/factory/{identifier}"`
  - `class FactoryConfigError(ValueError)`
  - `parse_factory_config(text: str) -> str`: o `test_command` sem espaços nas pontas

- [ ] **Step 1: Escrever os testes que falham**

```python
def test_repo_from_url_variants(self):
    for url in ("https://github.com/acme/app", "https://github.com/acme/app/",
                "https://github.com/acme/app.git", "https://github.com/acme/app/tree/main",
                "http://www.github.com/acme/app", "https://GitHub.com/acme/app#readme"):
        with self.subTest(url=url):
            self.assertEqual(repo_from_url(url), "acme/app")

def test_repo_from_url_rejects(self):
    for url in ("https://github.com/acme", "https://github.com/orgs/acme/projects/1",
                "https://github.com/settings/apps", "https://gitlab.com/acme/app",
                "https://github.com.evil.io/acme/app", "https://github.com/acme/app;rm",
                "https://github.com/acme/..", "github.com/acme/app", "ftp://github.com/acme/app"):
        with self.subTest(url=url):
            self.assertIsNone(repo_from_url(url))

def test_linked_repos_dedup_case_insensitive(self):
    self.assertEqual(
        linked_repos(["https://github.com/acme/app", "https://docs.google.com/x",
                      "https://github.com/Acme/App/issues"]),
        ["acme/app"],
    )

def test_linked_repos_two_repos(self):  # acme/app e acme/api → ["acme/app", "acme/api"]

def test_pinned_repos_only_factory_tree(self):
    self.assertEqual(
        pinned_repos(["https://github.com/acme/app/tree/factory/ENG-12",
                      "https://github.com/acme/app/pull/7",
                      "https://github.com/acme/api/tree/main"]),
        ["acme/app"],
    )

def test_pin_url(self):
    self.assertEqual(pin_url("acme/app", "ENG-12"), "https://github.com/acme/app/tree/factory/ENG-12")
    self.assertEqual(pinned_repos([pin_url("acme/app", "ENG-12")]), ["acme/app"])

def test_parse_factory_config(self):
    self.assertEqual(parse_factory_config('test_command = "  python -m pytest -q "\n'), "python -m pytest -q")
    self.assertEqual(parse_factory_config("test_command = 'npm test -- --grep \"a b\"'"), 'npm test -- --grep "a b"')

def test_parse_factory_config_errors(self):
    for text in ("", "test_command = ''", "test_command = 3", "test_command = [", "outro = 'x'"):
        with self.subTest(text=text), self.assertRaises(FactoryConfigError) as ctx:
            parse_factory_config(text)
        self.assertIn(".factory.toml", str(ctx.exception))
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_repos -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementar em `src/software_factory/orchestration/repos.py`**

Use `urllib.parse.urlsplit`. O scheme tem de ser `http` ou `https`, e o host (lower) `github.com` ou `www.github.com`. Os dois primeiros segmentos do path são owner e repo, e `.git` sai do fim do repo. O owner tem de casar `[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})` e não estar em `RESERVED_OWNERS = {"orgs", "apps", "settings", "marketplace", "features", "topics", "sponsors", "enterprises", "users", "login"}`. O repo tem de casar `[A-Za-z0-9_.-]{1,100}` e não ser `.` nem `..` (as regex com `fullmatch`). O pin exige que os segmentos 3 e 4 sejam `tree` e `factory`, com pelo menos mais um segmento depois. `parse_factory_config` usa `tomllib.loads`, e `TOMLDecodeError` vira `FactoryConfigError("`.factory.toml` inválido: <erro>")`. Sem `test_command` string não vazia, a mensagem é `"`.factory.toml` precisa de `test_command` (string não vazia)"`.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_repos -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/repos.py tests/test_repos.py
git commit -m "feat: regras do link do repo, do pin e do .factory.toml"
```

---

### Task 3: Activity `resolve_repo`

**Files:**
- Create: `src/software_factory/orchestration/repo_activities.py`
- Modify: `src/software_factory/orchestration/models.py` (novos dataclasses), `src/software_factory/github/client.py` (alias `GitHubClients`)
- Test: `tests/test_repo_activities.py`

**Interfaces:**
- Consumes: `repo_from_url`, `linked_repos`, `pinned_repos`, `parse_factory_config`, `FactoryConfigError`, `FACTORY_CONFIG_PATH` (Task 2); `GitHubClient.default_branch()`, `GitHubClient.get_file(path, ref)`; `LinearClient.execute(query, variables)`.
- Produces:
  - `models.RepoTarget(repo: str, test_command: str)` (frozen)
  - `models.RepoResolution(target: RepoTarget | None = None, problem: str = "")` (frozen)
  - `github/client.py`: `GitHubClients = Callable[[str], Awaitable[GitHubClient]]` (repo → client autenticado)
  - `RepoActivities(linear: LinearClient, clients: GitHubClients)`
  - `@activity.defn async RepoActivities.resolve_repo(issue: PlanIssueInput) -> RepoResolution`

- [ ] **Step 1: Confirmar o campo de links do projeto no schema do Linear**

Run (com `LINEAR_API_KEY` do `.env` carregada, sem imprimir a chave):
```bash
curl -s https://api.linear.app/graphql -H "Authorization: $LINEAR_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"{ __type(name: \"Project\") { fields { name type { name ofType { name } } } } }"}' \
  | python3 -c 'import json,sys; print([f["name"] for f in json.load(sys.stdin)["data"]["__type"]["fields"] if "ink" in f["name"]])'
```
Expected: a lista inclui `externalLinks` (conexão `EntityExternalLinkConnection`, nodes com `url`). Se só existir `links` (`ProjectLinkConnection`), use `links` na query abaixo e registre isso no docstring.

- [ ] **Step 2: Escrever os testes que falham**

`FakeLinear.execute` devolve `{"issue": {"project": {"name": "App", "externalLinks": {"nodes": [{"url": ...}]}} | None, "attachments": {"nodes": [{"url": ...}]}}}`. `FakeClient` registra `get_file(path, ref)` e devolve `config` (padrão `'test_command = "pytest"'`). `clients` é uma `async def` que registra o repo pedido. Rode com `ActivityEnvironment().run(RepoActivities(...).resolve_repo, ISSUE)`.

```python
async def test_resolves_from_project_link(self):
    self.assertEqual(result, RepoResolution(target=RepoTarget("acme/app", "pytest")))
    self.assertEqual(requested_repos, ["acme/app"])

async def test_reads_config_from_default_branch(self):
    self.assertEqual(client.files, [(".factory.toml", "main")])   # nunca factory/ENG-12

async def test_pin_wins_over_project(self):
    # anexo https://github.com/acme/old/tree/factory/ENG-12, projeto linka acme/app
    self.assertEqual(result.target.repo, "acme/old")

async def test_two_pins_is_problem(self):     # problem cita acme/a e acme/b; target None
async def test_no_project_is_problem(self):   # "não está em nenhum projeto"
async def test_no_github_link_is_problem(self):  # só docs.google.com; problem cita o nome do projeto "App"
async def test_two_repos_is_problem(self):    # problem cita acme/app e acme/api
async def test_same_repo_twice_resolves(self):   # /acme/app e /Acme/App/issues → acme/app
async def test_missing_config_is_problem(self):  # get_file → None; problem cita ".factory.toml" e "main"
async def test_invalid_config_is_problem(self):  # config "test_command = 3"; problem cita ".factory.toml"
async def test_no_access_is_problem(self):
    # clients levanta ApplicationError("GitHub App não está instalado em acme/app", non_retryable=True)
    self.assertIn("acme/app", result.problem); self.assertIsNone(result.target)
async def test_retryable_github_error_propagates(self):
    # default_branch levanta ApplicationError("GitHub indisponível") (retentável) → assertRaises
async def test_issue_not_found_is_non_retryable(self):  # {"issue": None} → ApplicationError non_retryable
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_repo_activities -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 4: Implementar**

Query única:
```graphql
query($id: String!) {
  issue(id: $id) {
    project { name externalLinks { nodes { url } } }
    attachments { nodes { url } }
  }
}
```
Ordem: pins (>1 → problema; 1 → repo) → projeto (None → problema) → `linked_repos` (0 ou >1 → problema) → `clients(repo)` → `default_branch()` → `get_file(FACTORY_CONFIG_PATH, default)` (None → problema) → `parse_factory_config` (`FactoryConfigError` → problema com a mensagem). Um `ApplicationError` com `non_retryable` em `clients`, `default_branch` ou `get_file` vira `f"sem acesso ao repo {repo} pelo GitHub: {error.message}"`, e o retentável propaga. Textos dos problemas (pt-BR, citando o projeto e a forma esperada do link):
- `"a issue não está em nenhum projeto do Linear; coloque-a num projeto com o link do repo nos Resources"`
- `f"o projeto {name} não tem link para um repo do GitHub nos Resources (https://github.com/<owner>/<repo>)"`
- `f"o projeto {name} tem links para mais de um repo do GitHub ({', '.join(repos)}); deixe só um"`
- `f"a issue tem anexos de branch factory/ em mais de um repo ({...}); remova os que não valem"`
- `f"o repo {repo} não tem {FACTORY_CONFIG_PATH} na branch default ({default})"`

- [ ] **Step 5: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_repo_activities -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/orchestration/repo_activities.py src/software_factory/orchestration/models.py src/software_factory/github/client.py tests/test_repo_activities.py
git commit -m "feat: activity resolve_repo (pin, projeto do Linear e .factory.toml)"
```

---

### Task 4: Token por repo nas activities do sandbox (ainda com o PAT)

Refactor sem mudança de comportamento visível, mais o conserto do token no `origin` do `clone_repo`. O repo ainda vem de `settings.target_repo`.

**Files:**
- Modify: `src/software_factory/github/client.py`, `src/software_factory/orchestration/activities.py` (`SandboxActivities.__init__`, `clone_repo`, `commit_plan`, `_authed_url`, `clone_branch`, `commit_implementation`), `src/software_factory/orchestration/review_activities.py:80-85`, `src/software_factory/orchestration/worker.py`
- Test: `tests/test_activities.py`, `tests/test_review_activities.py`

**Interfaces:**
- Consumes: `GitHubClients` (Task 3).
- Produces:
  - `github/client.py`: `TokenProvider = Callable[[str], Awaitable[str]]`, `static_token(token: str) -> TokenProvider`, `github_clients(tokens: TokenProvider) -> GitHubClients`
  - `SandboxActivities(settings, tokens: TokenProvider, sandbox_cls=..., usage_notifier=None, token_store=None)`
  - `SandboxActivities._authed_url(repo: str) -> str` (staticmethod, mesma string de hoje com `$GITHUB_TOKEN`)
  - `async SandboxActivities._token_env(repo: str) -> dict[str, str]` → `{"GITHUB_TOKEN": await tokens(repo)}`

- [ ] **Step 1: Escrever os testes que falham**

Em `tests/test_activities.py`, `SandboxActivities(SETTINGS, static_token("gh-token"), sandbox_cls=FakeSandbox)` em todo lugar. Testes novos:

```python
async def test_clone_repo_leaves_no_token_in_origin(self):
    await self.env.run(self.activities.clone_repo, "sbx-1")
    runs = FakeSandbox.instance.commands.runs
    self.assertEqual(runs[0]["envs"], {"GITHUB_TOKEN": "gh-token"})
    self.assertIn("git remote set-url origin https://github.com/acme/app.git", [r["cmd"] for r in runs])
    self.assertFalse(any("gh-token" in r["cmd"] for r in runs))

async def test_commit_plan_pushes_with_fresh_token(self):
    tokens = CountingTokens()   # devolve "tok-1", "tok-2", ...
    activities = SandboxActivities(SETTINGS, tokens, sandbox_cls=FakeSandbox)
    await env.run(activities.clone_repo, "sbx-1")
    await env.run(activities.commit_plan, CommitPlanRequest("sbx-1", "ENG-12"))
    push = next(r for r in runs if " push " in r["cmd"])
    self.assertIn("x-access-token:$GITHUB_TOKEN@github.com/acme/app.git", push["cmd"])
    self.assertEqual(push["envs"], {"GITHUB_TOKEN": "tok-2"})
    ls_remote = next(r for r in runs if "ls-remote" in r["cmd"])
    self.assertIn("x-access-token:$GITHUB_TOKEN@github.com/acme/app.git", ls_remote["cmd"])
```
Os testes existentes de `commit_plan` que esperam `push -u origin` passam a esperar o push na URL autenticada, `HEAD:refs/heads/factory/ENG-12`. Em `tests/test_review_activities.py`, o `git fetch` continua com `envs == {"GITHUB_TOKEN": "gh-token"}`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities tests.test_review_activities -v`
Expected: FAIL (`static_token` não existe / `SandboxActivities` não aceita `tokens`)

- [ ] **Step 3: Implementar**

`clone_repo` clona com `self._authed_url(repo)` e `envs=await self._token_env(repo)`, e depois roda `git remote set-url origin https://github.com/{repo}.git`, inclusive no retry em que `.git` já existe. `commit_plan` pede o token **uma vez**, no início, e usa esse `envs` no `ls-remote` e no `push ... HEAD:refs/heads/{branch}`, os dois na URL autenticada, como `commit_implementation` (que também pede uma vez só). `clone_branch`, `commit_implementation` e `review_pull` trocam `self.settings.github_token` por `await self._token_env(repo)`. Worker: `tokens = static_token(settings.github_token)` e `SandboxActivities(settings, tokens, ...)`.

- [ ] **Step 4: Rodar a suíte inteira**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/github/client.py src/software_factory/orchestration/activities.py src/software_factory/orchestration/review_activities.py src/software_factory/orchestration/worker.py tests/test_activities.py tests/test_review_activities.py
git commit -m "refactor: token do GitHub por repo nas activities; clone_repo sem token no origin"
```

---

### Task 5: `RepoTarget` nos workflows e activities; saem `TARGET_REPO` e `TEST_COMMAND`

**Files:**
- Create: `src/software_factory/orchestration/repo_resolution.py` (helper de workflow)
- Modify: `models.py`, `activities.py`, `review_activities.py`, `review_prompts.py` (`SENSITIVE_PATTERNS`), `github_activities.py`, `workflow.py`, `sessions.py`, `implement_workflow.py`, `review_workflow.py`, `worker.py`, `config.py`, `.env.example`, `README.md`, `CLAUDE.md`
- Test: `tests/test_workflow.py`, `tests/test_implement_workflow.py`, `tests/test_review_workflow.py`, `tests/test_sessions.py`, `tests/test_activities.py`, `tests/test_review_activities.py`, `tests/test_github_activities.py`, `tests/test_review_prompts.py`

**Interfaces:**
- Consumes: `RepoTarget`, `RepoResolution`, `RepoActivities.resolve_repo` (Task 3); `github_clients`, `TokenProvider` (Task 4).
- Produces:
  - Campo `target: RepoTarget` (sem default, depois do último campo obrigatório) em: `CommitPlanRequest`, `CloneBranchRequest`, `ImplementRequest`, `AnalyzeRequest`, `FixRequest`, `CommitImplementationRequest`, `PullRequestRequest`, `ReviewRequest`, `ReviewFixRequest`, `MergeRequest`
  - Novos: `CloneRepoRequest(sandbox_id: str, target: RepoTarget)`, `RunTestsRequest(sandbox_id: str, target: RepoTarget)`, `BranchRequest(identifier: str, target: RepoTarget)`
  - Activities com assinatura nova: `clone_repo(CloneRepoRequest)`, `run_tests(RunTestsRequest)`, `plan_exists(BranchRequest)`, `pull_request_info(BranchRequest)`
  - `GitHubActivities(clients: GitHubClients)`: um client por chamada, `await self.clients(request.target.repo)`
  - `repo_resolution.py`: `repo_problem_comment(problem: str) -> str`; `async resolve_target(issue: PlanIssueInput) -> RepoTarget`
  - `SessionWorkflow._target: RepoTarget`, preenchido pelo `run` antes de qualquer sessão. Os helpers (`_clone_branch`, `_commit`, `_implement_session`, `_analysis_session`) usam esse campo.
  - `FactorySettings` sem `target_repo` e `test_command`

- [ ] **Step 1: Escrever os testes de workflow que falham**

Os fakes de workflow ganham `@activity.defn(name="resolve_repo")`, que devolve `RepoResolution(target=TARGET)` com `TARGET = RepoTarget("acme/app", "pytest")`, ou `RepoResolution(problem="o projeto App não tem link...")` quando configurado. Se o fake ainda não tiver `post_comment`, ganha um.

```python
# tests/test_workflow.py
async def test_happy_path_passes_target(self):
    self.assertEqual(fake.calls[0], "resolve_repo")
    self.assertEqual(fake.clone_requests, [CloneRepoRequest("sbx-1", TARGET)])
    self.assertEqual(fake.commit_requests[0].target, TARGET)

async def test_repo_problem_comments_and_fails(self):
    fake = FakeActivities(repo_problem="o projeto App não tem link para um repo do GitHub")
    with self.assertRaises(WorkflowFailureError): await self.run_workflow(fake)
    self.assertEqual(fake.calls, ["resolve_repo", "post_comment"])   # nem move_issue, nem sandbox
    self.assertIn("o projeto App não tem link", fake.comments[0].body)

# tests/test_implement_workflow.py
async def test_target_reaches_every_repo_activity(self):
    # clone_branch, implement_plan, run_tests, commit_implementation, open_pull_request com target == TARGET
async def test_repo_problem_after_status_check(self):
    # calls começa com move_issue (checagem), resolve_repo, post_comment; sem plan_exists; workflow falha
async def test_skipped_issue_does_not_resolve(self):  # fora de In Progress → sem resolve_repo

# tests/test_review_workflow.py
async def test_target_reaches_review_activities(self):
    # pull_request_info, run_tests, review_pull, merge_pull_request com target == TARGET
async def test_repo_problem_after_status_check(self):  # idem implement
```

- [ ] **Step 2: Escrever os testes de activity que falham**

```python
# tests/test_activities.py
async def test_run_tests_uses_target_command(self):
    await env.run(activities.run_tests, RunTestsRequest("sbx-1", RepoTarget("acme/app", "npm test")))
    self.assertEqual(runs[-1]["cmd"], "npm test")
async def test_clone_branch_uses_target_repo(self):  # RepoTarget("acme/api", ...) → URLs com acme/api
async def test_implement_prompt_uses_target_command(self):  # prompt escrito contém "`npm test`"
def test_fix_prompt_forbids_changing_factory_toml(self):  # build_fix_prompt contém "`.factory.toml`"

# tests/test_github_activities.py
async def test_client_per_target_repo(self):
    # clients registra o repo; plan_exists(BranchRequest("ENG-12", RepoTarget("acme/api", "x"))) → ["acme/api"]

# tests/test_review_prompts.py
def test_factory_toml_is_sensitive(self):
    self.assertEqual(sensitive_files([".factory.toml", "src/a.py"]), (".factory.toml",))

# tests/test_activities.py (TestFactorySettings)
def test_from_env_without_target_repo_and_test_command(self):  # env sem as duas → ok
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: FAIL (`RepoTarget`/`CloneRepoRequest` não importáveis nos novos usos, `resolve_repo` não chamado)

- [ ] **Step 4: Implementar o helper de workflow em `repo_resolution.py`**

`resolve_target` executa `"resolve_repo"` (`start_to_close_timeout=60s`, `RetryPolicy(maximum_attempts=5)`, `result_type=RepoResolution`). Com `problem`, executa `"post_comment"` com `CommentRequest(issue.issue_id, str(workflow.uuid4()), repo_problem_comment(problem))` (30 s, 5 tentativas) e levanta `ApplicationError(f"{issue.identifier}: {problem}", non_retryable=True)`. Imports de models dentro de `workflow.unsafe.imports_passed_through()`, como em `token_reporting.py`. Texto do comentário:

```
**software-factory não sabe em qual repo trabalhar.**

{problem}

_Corrija (link do repo nos Resources do projeto, ou `.factory.toml` na branch default do repo) e dispare a etapa de novo._
```

- [ ] **Step 5: Ligar os workflows e as activities**

- `PlanIssueWorkflow.run`: `target = await resolve_target(issue)` **antes** do primeiro `move_issue`. `clone_repo` e `commit_plan` recebem o `target`.
- `ImplementIssueWorkflow.run` e `ReviewIssueWorkflow.run`: `self._target = await resolve_target(issue)` logo depois do `if not still_in_...: return`. O `target` vai em `plan_exists`, `open_pull_request`, `pull_request_info`, `merge_pull_request`, `review_pull`, `implement_review_fix` e nos três `run_tests` (`sessions.py` e as duas sessões de `review_workflow.py`).
- Activities: `request.target.repo` substitui `self.settings.target_repo`, e `request.target.test_command` substitui `self.settings.test_command`.
- `SENSITIVE_PATTERNS` ganha `".factory.toml"`. Em `activities.py:137`, "proibido mudar o `TEST_COMMAND`" vira "proibido mudar o `.factory.toml`".
- `config.py`: saem `target_repo` e `test_command` (campos e `from_env`). `worker.py`: `clients = github_clients(tokens)`, `GitHubActivities(clients)`, `RepoActivities(linear_client, clients)`, e `repo.resolve_repo` registrado.

- [ ] **Step 6: Rodar a suíte inteira**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 7: Atualizar a documentação**

- `.env.example`: tirar `TARGET_REPO` e `TEST_COMMAND`.
- `README.md`: tabela de envs; nova seção "Repo de cada projeto", com o link nos Resources do projeto e um `.factory.toml` de exemplo (`test_command = "uv pip install --system -e \".[dev]\" && python -m pytest"`); os passos que citam `TARGET_REPO` e `TEST_COMMAND`.
- `CLAUDE.md`: em "Stack" (Git/PR e Testes do repo alvo), a regra de resolução, o comentário de problema e onde resolve em cada workflow; `.factory.toml` na lista de `SENSITIVE_PATTERNS`; `repos.py`, `repo_activities.py` e `repo_resolution.py` na "Estrutura"; tirar `TARGET_REPO` e `TEST_COMMAND` dos "Comandos"; nota de deploy (nenhum workflow de issue rodando).

- [ ] **Step 8: Commit**

```bash
git add -A src tests .env.example README.md CLAUDE.md
git commit -m "feat: repo e comando de testes resolvidos por issue (projeto do Linear + .factory.toml)"
```

---

### Task 6: Pin do repo na issue depois do plano

**Files:**
- Modify: `src/software_factory/orchestration/models.py`, `src/software_factory/orchestration/linear_activities.py`, `src/software_factory/orchestration/workflow.py`, `src/software_factory/orchestration/worker.py`, `CLAUDE.md`
- Test: `tests/test_linear_activities.py`, `tests/test_workflow.py`

**Interfaces:**
- Consumes: `pin_url(repo, identifier)` (Task 2); `RepoTarget` (Task 3).
- Produces:
  - `models.AttachLinkRequest(issue_id: str, url: str, title: str, subtitle: str)`
  - `@activity.defn async LinearActivities.attach_link(request: AttachLinkRequest) -> None` (idempotente pela URL)

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_linear_activities.py
async def test_attach_link_creates(self):
    # 1ª query: issue.attachments.nodes = [] → mutation attachmentCreate com
    # input == {"issueId": "issue-1", "url": URL, "title": "factory/ENG-12", "subtitle": "acme/app"}
async def test_attach_link_existing_url_skips_mutation(self):
    # attachments.nodes = [{"url": URL}] → só 1 execute
async def test_attach_link_issue_not_found_non_retryable(self):
async def test_attach_link_unsuccessful_raises(self):  # success False → ApplicationError retentável

# tests/test_workflow.py
async def test_pins_repo_after_commit(self):
    self.assertEqual(fake.attachments, [AttachLinkRequest(
        "issue-1", "https://github.com/acme/app/tree/factory/ENG-12", "factory/ENG-12", "acme/app")])
    # ordem: ... commit_plan, kill_sandbox, attach_link, token_usage_report, ..., move_issue:In Progress
async def test_pin_failure_still_moves_to_in_progress(self):
    # attach_link levanta ApplicationError non_retryable → workflow termina com PlanResult e move para In Progress
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_linear_activities tests.test_workflow -v`
Expected: FAIL (`AttachLinkRequest` não existe)

- [ ] **Step 3: Implementar**

Query: `issue(id: $id) { attachments(first: 100) { nodes { url } } }`. Mutation: `attachmentCreate(input: $input) { success }`. No `PlanIssueWorkflow`, depois do `finally` e antes de `publish_token_usage`, roda `attach_link` (30 s, `RetryPolicy(maximum_attempts=5)`) dentro de `try/except ActivityError`, com `workflow.logger.warning` em caso de falha. Registrar `linear.attach_link` no worker.

- [ ] **Step 4: Rodar a suíte inteira**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Documentar e commitar**

`CLAUDE.md`: o pin é gravado depois do commit do plano e vence o link do projeto. Para trocar o repo de uma issue já planejada, apague o anexo `factory/<ID>`.

```bash
git add src tests CLAUDE.md
git commit -m "feat: anexo factory/<ID> fixa o repo da issue depois do plano"
```

---

### Task 7: Troca do PAT pelo GitHub App

**Files:**
- Modify: `src/software_factory/config.py`, `src/software_factory/orchestration/worker.py`, `src/software_factory/github/client.py` (sai `static_token`), `.env.example`, `README.md`, `CLAUDE.md`
- Test: `tests/test_activities.py` (`TestFactorySettings`), testes que usavam `static_token` (trocar por um helper local `async def tokens(repo): return "gh-token"`)

**Interfaces:**
- Consumes: `GitHubApp(app_id, private_key).installation_token` (Task 1) como `TokenProvider` (Task 4).
- Produces: `FactorySettings.github_app_id: str` e `FactorySettings.github_app_private_key: str` (conteúdo PEM). Sai `github_token`.

- [ ] **Step 1: Escrever os testes que falham**

```python
def test_from_env_reads_app_private_key_file(self):
    # tempfile com "-----BEGIN RSA PRIVATE KEY-----..."; env GITHUB_APP_ID=123, GITHUB_APP_PRIVATE_KEY_PATH=<arquivo>
    self.assertEqual(settings.github_app_id, "123")
    self.assertTrue(settings.github_app_private_key.startswith("-----BEGIN"))
def test_from_env_requires_app_id(self):   # sem GITHUB_APP_ID → RuntimeError "GITHUB_APP_ID não definida"
def test_from_env_missing_key_file(self):  # caminho inexistente → RuntimeError citando GITHUB_APP_PRIVATE_KEY_PATH
def test_from_env_no_longer_needs_github_token(self):
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
Expected: FAIL (`github_app_id` não existe)

- [ ] **Step 3: Implementar**

`from_env` lê `Path(_required("GITHUB_APP_PRIVATE_KEY_PATH")).read_text()`, e `OSError` vira `RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH ilegível: {erro}")`. Worker: `app = GitHubApp(settings.github_app_id, settings.github_app_private_key)` e `tokens = app.installation_token`. Tirar `static_token` de `github/client.py`.

- [ ] **Step 4: Rodar a suíte inteira**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK

- [ ] **Step 5: Documentar**

- `README.md`: seção "GitHub App". O App é criado na conta/org dos repos, sem webhook, com as permissões **Contents: Read and write**, **Pull requests: Read and write** e **Metadata: Read**. Depois: instalar só nos repos da fábrica, gerar e baixar a private key e preencher `GITHUB_APP_ID` e `GITHUB_APP_PRIVATE_KEY_PATH`. A tabela de envs perde `GITHUB_TOKEN`. `.env.example` idem.
- `CLAUDE.md` "Stack" (Git/PR): GitHub App no lugar do PAT; PyJWT; token de instalação restrito ao repo da issue, com validade de 1h e renovado com 10 min de margem; a instalação é a allowlist de repos. Ajustar a nota "o token deve ser tratado como exposto", porque agora o alcance é um repo por até 1h. "Comandos": as envs do worker.

- [ ] **Step 6: Verificação manual ponta a ponta**

Com o App instalado num repo de teste que tenha `.factory.toml` na branch default, e um projeto do Linear com o link desse repo nos Resources: suba `temporal server start-dev`, `software-factory-worker` e `software-factory`, e crie uma issue com label `Feature` nesse projeto.
Expected: o plano é commitado em `factory/<ID>` **nesse** repo; a issue ganha o anexo `factory/<ID>` e vai para `In Progress`. Repita com uma issue num projeto sem link: a issue recebe o comentário "não sabe em qual repo trabalhar", continua em `To Do`, e o workflow aparece como falho na UI do Temporal.

- [ ] **Step 7: Commit**

```bash
git add src tests .env.example README.md CLAUDE.md
git commit -m "feat: GitHub App com token de instalação por repo no lugar do GITHUB_TOKEN"
```
