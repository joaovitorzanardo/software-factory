# Review automática, loop de correção e merge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando a issue entra em `In Review`, um novo `ReviewIssueWorkflow` revisa o diff da PR `factory/<ID>`, decide sozinho o que fazer com cada achado (corrigir agora, abrir issue nova, só registrar, descartar ou, em último caso, perguntar) e repete corrigir → testar → revisar até a review aprovar. Depois faz o merge da PR e move a issue para `Done`.

**Architecture:** O workflow é disparado pelo webhook do Linear quando o status muda para `In Review`, como o `ImplementIssueWorkflow` é disparado por `In Progress`. O próprio `ImplementIssueWorkflow` faz essa mudança, então a cadeia segue sem passo manual. A sessão de review usa um sandbox. Nele rodam **duas lentes em paralelo**, em contextos separados do Claude: `correctness`, com o template `code-reviewer.md` do `requesting-code-review`, e `security`, com um prompt próprio. Depois roda uma **triagem** com a skill `receiving-code-review`: ela confere cada achado no código e grava `review.json` com uma decisão por achado. O workflow faz o que o JSON pede. A sessão de correção da review é parecida com a de correção de testes. Se os testes ficarem vermelhos depois dela, o workflow reaproveita o loop analisar → corrigir que já existe, extraído para uma classe base comum aos dois workflows.

**Tech Stack:** Python 3.12, temporalio, e2b `AsyncSandbox`, httpx (REST do GitHub e GraphQL do Linear), FastAPI, unittest. Nenhuma dependência nova.

**Spec:** pedido do usuário em 2026-11-14: "review, depois de passar a issue para o status In Review, voce deve fazer uma review completa do codigo novo. [...] quais skills ja tem prontas que fazem a review? Devo separar em uma skill para seguranca outra para estilo...? Depois de feita a review, se estiver tudo ok e aprovado, fazer o merge e passar a issue para done, se nao, entrar em um loop de: corrigir os problemas apontados pela review, rodar a review novamente. Assim como na implementação, caso algo muito grande deva ser alterado, adicionar o comentario na issue e esperar a minha resposta. Tente ser o mais autonomo possivel nas resolucoes. Avalie racionalmente os pontos, pondere sobre os trade-offs e então tome uma decisão. Alguns pontos por exemplo podem ser reais mas não tão importantes no momento atual, nesses casos voce pode adicionar um comentário na issue, criar uma issue nova..."

## Decisões tomadas neste plano (não estão na spec)

**Skills de review que já existem e o que fazer com cada uma:**

| Skill | O que faz | Uso aqui |
|---|---|---|
| `superpowers:requesting-code-review` (7.0.2) | Template `code-reviewer.md`: aderência ao plano, qualidade, arquitetura, testes e prontidão. Achados em Critical/Important/Minor com `file:line` e veredito | **Vendorizada.** É a lente `correctness` |
| `superpowers:receiving-code-review` (7.0.2) | Como receber uma review: conferir cada item no código, recusar com argumento técnico o que estiver errado e não concordar só por concordar | **Vendorizada.** É a triagem, onde se "avalia racionalmente e pondera trade-offs" |
| `/security-review` (embutida no Claude Code) | Review de segurança do diff da branch | Não usada. Depende da versão do CLI no template do E2B, compara com `origin/HEAD` (o clone raso não tem) e não deixa controlar o formato da saída. A lente `security` faz o mesmo com um prompt próprio |
| `/code-review`, `/simplify` (embutidas) | Bugs de correção; limpeza com correção automática | Não usadas. `/simplify` aplica mudanças, e a review tem de ser só leitura. `/code-review` cobre o mesmo que a lente `correctness` sem formato controlável |
| `bmad-code-review` / `bmad-review` | Várias lentes em paralelo | Não usadas. Dependem da instalação e da configuração do BMad |

**Separar segurança e estilo?** Segurança **sim**, como lente própria em contexto separado. Um revisor genérico costuma dar pouco peso a segurança, e aqui o código foi escrito por um agente com `Bash`. Estilo **não**: formatação e lint são determinísticos e mais baratos num linter. O lugar deles é o `TEST_COMMAND` do repo alvo. Problemas de legibilidade e manutenção que um linter não pega entram na lente `correctness` como `minor`. As duas lentes rodam **no mesmo sandbox, em paralelo** (dois `claude -p` ao mesmo tempo, só leitura). Assim o custo é de um sandbox, cada lente tem o próprio contexto e o orquestrador controla quais lentes rodam. Não depende de subagentes dentro do Claude.

**Decisões por achado (triagem)**, escritas em `review.json`. São regras do prompt, e o workflow só executa o que vier:

- `fix`: o problema foi confirmado no código, é `critical` ou `important` e foi introduzido pela PR ou está no escopo da issue. Bloqueia o merge.
- `issue`: é real, mas fica fora do escopo da issue, é código antigo que a PR não tocou ou o custo de corrigir agora não compensa, e mesmo assim merece acompanhamento. O workflow cria uma issue nova no Linear.
- `note`: é real e pequeno (`minor`). Só aparece no comentário de resumo da rodada.
- `reject`: é falso positivo ou está errado. A razão técnica fica registrada.
- `ask`: só para decisão grande, com os mesmos critérios da análise de falha (mudar escopo ou requisitos, mudar a arquitetura do plano, mudar o comportamento esperado por testes, adicionar dependência, migração de dados). Vira pergunta no Linear.
- Achado `minor` nunca vira `fix`. Isso garante que o loop converge e que nitpick não segura o merge. Segurança `critical` nunca vira `note` nem `issue`: ou `fix`, ou `ask`, ou `reject` com prova.

**Outras decisões:**

- **Gatilho por webhook (`In Review`)** em vez de estender o `ImplementIssueWorkflow`. É o mesmo padrão de planejamento → implementação. A história de cada workflow fica separada, e uma issue movida à mão para `In Review` também passa pela review, desde que exista PR aberta de `factory/<ID>`.
- **Issues de follow-up sem label de gatilho** (`Feature`/`Bug`). Assim uma review não dispara sozinha planejamento → implementação → review, o que viraria uma cadeia sem fim. A pessoa adiciona a label se quiser que a factory pegue a issue. A issue nasce no time da issue de origem, com link para ela, com id gerado pelo cliente (idempotente).
- **Resumo da rodada como comentário no Linear** (`post_comment`, que já é idempotente), com cada achado, a decisão, a razão e os links das issues criadas. Não comenta na PR do GitHub (YAGNI).
- **Merge squash** (`PUT /pulls/{n}/merge`, `merge_method="squash"`, `sha` = commit revisado). Os commits `wip:` somem do histórico da branch default, e o merge só acontece se o head ainda for o que foi aprovado. A branch `factory/<ID>` não é apagada: use a opção "Automatically delete head branches" do repo.
- **PR que não pode ser mergeada** (conflito, branch protection, método de merge proibido): o workflow pergunta no Linear com o motivo. Depois da resposta, revisa de novo, porque a pessoa pode ter empurrado commits. **Head mudou desde a review:** revisa de novo, sem perguntar. Branch protection que exige aprovação bloqueia sempre, porque o autor da PR é o dono do token. O README registra isso.
- **Trava de custo:** depois de `MAX_REVIEW_ROUNDS = 3` rodadas de correção da review sem aprovar, o workflow pergunta como seguir. A resposta zera o contador. Uma sessão de review que falha conta como rodada.
- **Parar:** mover a issue para fora de `In Review`. O status é checado antes de cada rodada e na espera por resposta. Resultado `OUTCOME_STOPPED`, sem merge.
- **Re-review do diff inteiro da PR** (merge-base → head) a cada rodada, com o histórico de todas as rodadas no prompt (achados, decisões e notas da correção). Assim um achado `reject`/`issue`/`note` não volta sem evidência nova.
- **Respostas na thread:** `handle_reply` manda o signal `human_answer` para `implement-issue-<id>` **e** para `review-issue-<id>`. O workflow que não tem a pergunta pendente ignora.

## Global Constraints

- Nenhuma dependência nova. GitHub via `GitHubClient` (httpx); Linear via `LinearClient` (GraphQL).
- Workflow determinístico: ids de comentário e de issue nova com `workflow.uuid4()`; espera com `workflow.wait_condition`.
- Status novo: `STATE_DONE = "Done"`. O `ReviewIssueWorkflow` valida no início (`require_states=(STATE_DONE,)`) e falha cedo se ele não existir.
- Id do workflow: `review-issue-<issue.id>`. Input: `PlanIssueInput`. Saída: `ReviewResult`.
- Constantes em `review_workflow.py`: `MAX_REVIEW_ROUNDS = 3`, `FINDING_DETAIL_CAP = 1000`.
- Arquivos da review fora do repo, nunca commitados: `REVIEW_DIR = "/home/user/review"`, `CORRECTNESS_PATH = f"{REVIEW_DIR}/correctness.md"`, `SECURITY_PATH = f"{REVIEW_DIR}/security.md"`, `REVIEW_JSON_PATH = f"{REVIEW_DIR}/review.json"`.
- Orçamento do sandbox de review (55 min): clone + fetch do merge-base + lentes em paralelo (`REVIEW_LENS_TIMEOUT_S = 12 * 60` cada) + triagem (`REVIEW_TRIAGE_TIMEOUT_S = 10 * 60`); activity `review_pull` com 30 min, `heartbeat_timeout` de 2 min e `maximum_attempts=1`. Sessão de correção da review: mesmos timeouts do `implement_fix` (comando 30 min, activity 35 min, `maximum_attempts=1`) + `run_tests` + `commit_implementation`.
- Lentes e triagem com `--allowedTools "Read,Grep,Glob,Skill,Bash,Write"` (Bash para `git diff`/`git log`); a correção com `"Read,Grep,Glob,Skill,Edit,Write,Bash"`. O token nunca vai no `origin` nem nos prompts. O fetch do merge-base usa `envs={"GITHUB_TOKEN": ...}` como o `commit_implementation`.
- Prompt de correção da review inclui `_NO_TEST_TAMPERING`. A lente `security` também procura testes afrouxados ou apagados.
- Mensagens de commit: verde `fix: review <n> de <ID>`; vermelho `wip: review <n> de <ID> (testes vermelhos)`; falha da sessão `wip: review <n> de <ID> (correção falhou)`.
- Logs, mensagens, prompts e docs em português.

## Review Focus

1. A review nunca aprova porque sempre aparece um achado novo: `minor` nunca é `fix`, achados antigos com decisão não voltam, e `MAX_REVIEW_ROUNDS` pergunta. Testes na Task 4 (`parse_review` rejeita `minor`+`fix`) e na Task 5 (`test_stuck_after_max_review_rounds_asks_human`).
2. Merge de código que não foi revisado (push entre a review e o merge): o merge manda `sha` do commit revisado, e um head diferente faz revisar de novo. Testes na Task 2 (`test_merge_pull_head_mismatch`) e na Task 5 (`test_head_changed_since_review_rereviews`).
3. Retry de `create_issue` ou replay do workflow criando issue duplicada: o id vem de `workflow.uuid4()` e há consulta antes de criar. Testes na Task 3 (`test_retry_after_create_is_noop`) e na Task 5 (`test_issue_findings_create_follow_up_issues_once`).
4. Issue de follow-up dispara a factory sozinha: é criada sem labels. Teste na Task 3 (`test_creates_issue_without_labels`).
5. Retry do merge depois de um merge que já aconteceu (timeout de rede) falha com 405: `merge_pull_request` consulta a PR antes e devolve `merged=True` se já foi mergeada. Teste na Task 2 (`test_merge_already_merged_is_noop`).

---

### Task 1: Classe base de sessões (refactor sem mudar comportamento)

Extrai do `ImplementIssueWorkflow` os helpers de sessão, a pergunta humana e o loop de testes vermelhos, com o status parametrizado. O `ReviewIssueWorkflow` reaproveita tudo isso.

**Files:**
- Create: `src/software_factory/orchestration/sessions.py`
- Modify: `src/software_factory/orchestration/implement_workflow.py`
- Test: `tests/test_implement_workflow.py` (sem mudanças: é a rede de segurança), `tests/test_sessions.py`

**Interfaces:**
- Produces (em `sessions.py`; as constantes e `failure_message`, `format_question`, `stuck_question` saem de `implement_workflow.py` e são reexportadas por ele):

```python
class SessionWorkflow:  # sem @workflow.defn; as subclasses declaram o signal
    _plan_sha: str | None; _clarifications: list[Clarification]
    _pending_question: str | None; _answer: str | None
    def _receive_answer(self, answer: HumanAnswer) -> None  # corpo atual do signal
    async def _move(self, request: MoveIssueRequest) -> bool
    async def _still_in(self, issue: PlanIssueInput, state: str) -> bool  # era _still_in_progress
    async def _create_sandbox(self, issue) -> str
    async def _clone_branch(self, sandbox_id, issue) -> str
    async def _kill_sandbox(self, sandbox_id) -> None
    async def _commit(self, sandbox_id, issue, base_sha, message, require_changes) -> None
    async def _implement_session(self, issue, attempt: int, fix: FixAnalysis | None = None) -> TestRunResult
    async def _analysis_session(self, issue, test_output, attempts) -> FixAnalysis | str
    async def _ask(self, issue, question: str, state: str, label: str | None = None) -> bool
    async def _fix_until_green(self, issue, tests: TestRunResult, state: str) -> TestRunResult | None
        # o while atual do run(); None = issue saiu de `state` (parou)

def format_question(question: str, state: str) -> str  # "Para parar, mova a issue para fora de {state}."
```

- `ImplementIssueWorkflow(SessionWorkflow)` mantém `@workflow.signal def human_answer(self, answer)` → `self._receive_answer(answer)`, e o `run` vira: guardas → `_implement_session(issue, 0)` → `_fix_until_green(issue, tests, STATE_IN_PROGRESS)` (None → `OUTCOME_STOPPED`) → PR → `In Review`.
- O texto de `format_question` com `state=STATE_IN_PROGRESS` continua igual ao de hoje, palavra por palavra, exceto pelo trecho do status. O texto "para seguir com a correção dos testes" vira "para seguir".

- [ ] **Step 1: Escrever `tests/test_sessions.py`** com `test_format_question_names_state`: `format_question("Q?", "In Review")` contém `"Q?"`, `"Responda **nesta thread**"` e `"Para parar, mova a issue para fora de In Review."`.
- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_sessions -v` → FAIL (`ModuleNotFoundError: software_factory.orchestration.sessions`).
- [ ] **Step 3: Criar `sessions.py`**, movendo o código com as assinaturas acima, e enxugar o `ImplementIssueWorkflow`. Os nomes das activities e as políticas de retry não mudam.
- [ ] **Step 4: Rodar tudo** — `PYTHONPATH=src python3 -m unittest discover -s tests` → PASS. Todos os testes de `test_implement_workflow.py` passam sem edição.
- [ ] **Step 5: Commit** — `git commit -m "refactor: extrai sessões, pergunta humana e loop de testes para SessionWorkflow"`

---

### Task 2: Modelos, cliente do GitHub e activities de PR/merge

**Files:**
- Modify: `src/software_factory/orchestration/models.py`
- Modify: `src/software_factory/github/client.py`
- Modify: `src/software_factory/orchestration/github_activities.py`
- Test: `tests/test_github_client.py`, `tests/test_github_activities.py`

**Interfaces:**
- Produces (em `models.py`):

```python
STATE_DONE = "Done"
OUTCOME_MERGED = "merged"
OUTCOME_NO_PR = "no_pr"
REVIEW_DECISIONS = ("fix", "issue", "note", "reject", "ask")
REVIEW_SEVERITIES = ("critical", "important", "minor")
REVIEW_LENSES = ("correctness", "security")

@dataclass(frozen=True)
class PullInfo:
    number: int; url: str; head_sha: str; base_ref: str
    base_sha: str  # merge-base entre base_ref e head (compare API)

@dataclass(frozen=True)
class ReviewFinding:
    lens: str; severity: str; file: str; line: int | None
    title: str; detail: str; decision: str; reason: str

@dataclass(frozen=True)
class ReviewRound:
    number: int; summary: str
    findings: tuple[ReviewFinding, ...] = ()
    fix_notes: str = ""  # resumo da sessão de correção (o que corrigiu e o que não, e por quê)

@dataclass(frozen=True)
class ReviewRequest:
    sandbox_id: str; issue: PlanIssueInput; base_sha: str
    history: tuple[ReviewRound, ...] = (); clarifications: tuple[Clarification, ...] = ()

@dataclass(frozen=True)
class ReviewReport:
    summary: str; findings: tuple[ReviewFinding, ...]

@dataclass(frozen=True)
class ReviewFixRequest:
    sandbox_id: str; issue: PlanIssueInput; round: int
    findings: tuple[ReviewFinding, ...]; clarifications: tuple[Clarification, ...] = ()

@dataclass(frozen=True)
class MergeRequest:
    identifier: str; title: str; number: int; head_sha: str

@dataclass(frozen=True)
class MergeResult:
    merged: bool
    head_changed: bool = False
    reason: str = ""

@dataclass(frozen=True)
class CreateIssueRequest:
    origin_issue_id: str; new_issue_id: str  # UUID v4 do workflow
    title: str; description: str

@dataclass(frozen=True)
class FollowUpIssue:
    identifier: str; url: str

@dataclass(frozen=True)
class ReviewResult:
    identifier: str; outcome: str; pr_url: str | None = None
    follow_ups: tuple[str, ...] = ()  # identifiers das issues criadas
```

- Produces (em `GitHubClient`):
  - `find_open_pull(head: str) -> dict[str, Any] | None`: passa a devolver o JSON da PR, e não só a URL. `open_pull_request` usa `["html_url"]`.
  - `get_pull(number: int) -> dict[str, Any]`
  - `merge_base(base: str, head: str) -> str`: `GET /repos/{repo}/compare/{base}...{head}` → `["merge_base_commit"]["sha"]`.
  - `merge_pull(number: int, sha: str, title: str) -> tuple[bool, str]`: `PUT /repos/{repo}/pulls/{number}/merge` com `{"merge_method": "squash", "sha": sha, "commit_title": title}`; 200 → `(True, "")`; 405/409 → `(False, message)`; outros códigos passam por `_check`.
- Produces (em `GitHubActivities`):
  - `pull_request_info(identifier: str) -> PullInfo | None`: PR aberta de `factory/<ID>`, ou None.
  - `merge_pull_request(request: MergeRequest) -> MergeResult`: chama `get_pull` antes. `merged` → `MergeResult(True)`. Fechada sem merge → `(False, reason="PR fechada sem merge")`. `head.sha != request.head_sha` → `(False, head_changed=True)`. `mergeable is None` → `ApplicationError` **retentável** (o GitHub ainda está calculando). `mergeable is False` → `(False, reason=f"não mergeável ({mergeable_state})")`. Nos outros casos chama `merge_pull` com o título `f"{identifier}: {title} (#{number})"`, e um 409 vira `head_changed=True`.

- [ ] **Step 1: Escrever os testes do cliente** em `tests/test_github_client.py`, no estilo `MockTransport` que já existe: `test_find_open_pull_returns_pull` (devolve dict com `html_url`), `test_get_pull`, `test_merge_base` (path `/repos/acme/app/compare/main...abc`, devolve o sha), `test_merge_pull_ok` (body com `merge_method == "squash"` e `sha`), `test_merge_pull_not_mergeable` (405 com `message` → `(False, "Pull Request is not mergeable")`), `test_merge_pull_head_mismatch` (409 → `(False, msg)`). Atualizar `test_find_open_pull`.
- [ ] **Step 2: Escrever os testes das activities** em `tests/test_github_activities.py`: `test_pull_request_info` (monta `PullInfo` com o merge-base), `test_pull_request_info_none_without_open_pull`, `test_merge_already_merged_is_noop` (sem PUT), `test_merge_head_changed` (head ≠ → `head_changed=True`, sem PUT), `test_merge_mergeable_unknown_is_retryable` (`ApplicationError`, `non_retryable` False), `test_merge_conflict_returns_reason`, `test_merge_squash_with_reviewed_sha`. Ajustar o teste de `open_pull_request` que reaproveita PR aberta.
- [ ] **Step 3: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_github_client tests.test_github_activities -v` → FAIL.
- [ ] **Step 4: Implementar** os modelos, os métodos do cliente e as activities.
- [ ] **Step 5: Rodar tudo** — `PYTHONPATH=src python3 -m unittest discover -s tests` → PASS.
- [ ] **Step 6: Commit** — `git commit -m "feat: modelos da review e merge squash da PR com sha revisado"`

---

### Task 3: Activity `create_issue` no Linear (follow-up idempotente)

**Files:**
- Modify: `src/software_factory/orchestration/linear_activities.py`
- Test: `tests/test_linear_activities.py`

**Interfaces:**
- Consumes: `CreateIssueRequest`, `FollowUpIssue` (Task 2).
- Produces: `LinearActivities.create_issue(request: CreateIssueRequest) -> FollowUpIssue`. Faz três chamadas:
  1. `issues(filter: {id: {eq: $id}}) { nodes { identifier url } }`: se a issue já existe, devolve sem mutation.
  2. `issue(id: $originId) { team { id } }`: issue de origem ausente → `ApplicationError(non_retryable=True)`.
  3. `issueCreate(input: {id, teamId, title, description}) { success issue { identifier url } }`, **sem** `labelIds`. `success` falso → `ApplicationError` retentável.

- [ ] **Step 1: Escrever os testes** (fake de `LinearClient` como nos testes de `post_comment`): `test_creates_issue_in_origin_team` (o input tem `id == request.new_issue_id` e `teamId == "team-1"`), `test_creates_issue_without_labels` (`"labelIds" not in input`), `test_retry_after_create_is_noop` (nenhuma mutation e devolve o `FollowUpIssue` existente), `test_missing_origin_is_non_retryable`, `test_unsuccessful_create_is_retryable`.
- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_linear_activities -v` → FAIL.
- [ ] **Step 3: Implementar `create_issue`** com as queries/mutation como constantes do módulo, seguindo o padrão de `post_comment`.
- [ ] **Step 4: Rodar e ver passar** (mesmo comando) → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: activity create_issue para follow-ups da review"`

---

### Task 4: Sessão de review no sandbox (lentes + triagem) e vendorização das skills

**Files:**
- Create: `src/software_factory/orchestration/skills/requesting-code-review/{SKILL.md,code-reviewer.md,LICENSE}` e `src/software_factory/orchestration/skills/receiving-code-review/{SKILL.md,LICENSE}`, copiados de superpowers 7.0.2 (`~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.2/skills/`). A LICENSE é a mesma (MIT) das outras skills vendorizadas.
- Create: `src/software_factory/orchestration/review_prompts.py` (prompts e parser da review, para o `activities.py` não crescer mais)
- Modify: `src/software_factory/orchestration/activities.py` (activities `review_pull`, `implement_review_fix`)
- Test: `tests/test_review_prompts.py`, `tests/test_activities.py`

**Interfaces:**
- Consumes: `ReviewRequest`, `ReviewReport`, `ReviewFinding`, `ReviewRound`, `ReviewFixRequest` e as constantes `REVIEW_*` (Task 2); `_fence`, `_issue_section`, `_clarifications_section`, `_NO_TEST_TAMPERING`, `install_skills`, `_run_with_heartbeat`, `parse_result`, `plan_path` (de `activities.py`; `review_prompts.py` importa de lá).
- Produces (em `review_prompts.py`):
  - `build_correctness_prompt(request: ReviewRequest) -> str`: manda ler `~/.claude/skills/requesting-code-review/code-reviewer.md` e **fazer a review descrita no template sem despachar subagente**, com DESCRIPTION = a issue, PLAN_OR_REQUIREMENTS = `plan_path(ID)` + issue, BASE_SHA = `request.base_sha` e HEAD_SHA = `HEAD`. Salva em `CORRECTNESS_PATH`. Não altera o repo.
  - `build_security_prompt(request: ReviewRequest) -> str`: diff `base_sha..HEAD`. O checklist cobre injeção (shell/SQL/template), segredos em código ou log, validação de entrada vinda de fora, path traversal, SSRF, desserialização insegura, authn/authz, dependência nova e **testes apagados, pulados ou afrouxados**. Mesmo formato Critical/Important/Minor com `file:line`. Salva em `SECURITY_PATH`.
  - `build_triage_prompt(request: ReviewRequest) -> str`: usar a skill `receiving-code-review`; ler os dois relatórios; **conferir cada item no código**; aplicar as regras de decisão da seção "Decisões por achado" deste plano, copiadas no prompt; não repetir achados do histórico que já têm decisão, salvo evidência nova; achados `fix` de rodadas anteriores que continuam abertos entram de novo. Grava `REVIEW_JSON_PATH` no schema `{"summary": str, "findings": [{"lens","severity","file","line","title","detail","decision","reason"}]}`. Achado com `decision == "ask"` traz em `detail` a pergunta, as opções e a recomendação. Inclui o histórico (rodadas com achados, decisões e `fix_notes`) e as clarificações.
  - `build_review_fix_prompt(request: ReviewFixRequest, test_command: str) -> str`: corrigir **só** os achados listados, com TDD; usar a skill `receiving-code-review` (se um achado se mostrar errado ao implementar, não corrigir e explicar no resumo final); `_NO_TEST_TAMPERING`; commits locais e sem push; o resumo final diz, achado por achado, o que foi feito.
  - `parse_review(text: str) -> ReviewReport`: JSON inválido, campo ausente, valor fora de `REVIEW_DECISIONS`/`REVIEW_SEVERITIES`/`REVIEW_LENSES`, `minor` com `decision == "fix"`, ou segurança `critical` com decisão `note`/`issue` → `ApplicationError` (non_retryable). `detail` e `reason` truncados em `FINDING_DETAIL_CAP = 1000`; `line` aceita `null`.
- Produces (em `SandboxActivities`):
  - `review_pull(request: ReviewRequest) -> ReviewReport`, nesta ordem: `install_skills`; `rm -rf REVIEW_DIR && mkdir -p REVIEW_DIR`; `git fetch --depth 1 <authed_url> <base_sha>` com `envs={"GITHUB_TOKEN": ...}`; grava os três prompts em `/home/user/review-*.md`; roda as duas lentes **em paralelo** (`asyncio.gather` de dois `_run_with_heartbeat`, `REVIEW_LENS_TIMEOUT_S`, `parse_result` de cada); roda a triagem (`REVIEW_TRIAGE_TIMEOUT_S`); devolve `parse_review(conteúdo de REVIEW_JSON_PATH)`. Arquivo ausente → `ApplicationError`.
  - `implement_review_fix(request: ReviewFixRequest) -> str`: `install_skills`, grava o prompt, roda com `IMPLEMENT_COMMAND_TIMEOUT_S` e devolve `parse_result` (as `fix_notes`).

- [ ] **Step 1: Vendorizar as duas skills** (cópia literal + LICENSE) e conferir com `ls src/software_factory/orchestration/skills/`, que deve listar 4 diretórios.
- [ ] **Step 2: Escrever `tests/test_review_prompts.py`**: `test_parse_review_valid` (2 achados → `ReviewReport` com tuplas), `test_parse_review_rejects_unknown_decision`, `test_parse_review_rejects_minor_fix`, `test_parse_review_rejects_deferred_critical_security`, `test_parse_review_truncates_detail` (2000 chars → 1000), `test_parse_review_invalid_json`, `test_triage_prompt_includes_history_and_rules` (contém os 5 nomes de decisão, `receiving-code-review`, o título de um achado do histórico, as `fix_notes` e a resposta de uma clarificação), `test_correctness_prompt_uses_template_and_base_sha`, `test_security_prompt_checks_test_tampering` (contém "afrouxados"), `test_review_fix_prompt_forbids_test_tampering_and_push`.
- [ ] **Step 3: Escrever os testes das activities** em `tests/test_activities.py` (com o fake de sandbox que já existe): `test_review_pull_runs_lenses_in_parallel_then_triage` (os dois comandos de lente começam antes de qualquer um terminar, e a triagem vem por último), `test_review_pull_fetches_base_with_token_env_only` (o token está em `envs` e não aparece no comando nem nos prompts gravados), `test_review_pull_missing_json_raises`, `test_implement_review_fix_returns_notes`, `test_install_skills_copies_review_skills`.
- [ ] **Step 4: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_review_prompts tests.test_activities -v` → FAIL.
- [ ] **Step 5: Implementar** `review_prompts.py` e as duas activities.
- [ ] **Step 6: Rodar tudo** — `PYTHONPATH=src python3 -m unittest discover -s tests` → PASS.
- [ ] **Step 7: Commit** — `git commit -m "feat: sessão de review com lentes correctness/security e triagem receiving-code-review"`

---

### Task 5: `ReviewIssueWorkflow`

**Files:**
- Create: `src/software_factory/orchestration/review_workflow.py`
- Test: `tests/test_review_workflow.py`

**Interfaces:**
- Consumes: `SessionWorkflow` (Task 1); activities `pull_request_info`, `merge_pull_request` (Task 2), `create_issue` (Task 3), `review_pull`, `implement_review_fix` (Task 4), mais `move_issue`, `post_comment`, `create_sandbox`, `clone_branch`, `run_tests`, `commit_implementation`, `kill_sandbox`, `analyze_failure`, `implement_fix`, que já existem.
- Produces: `ReviewIssueWorkflow(SessionWorkflow)` com `@workflow.signal human_answer(answer: HumanAnswer)` e `@workflow.run run(issue: PlanIssueInput) -> ReviewResult`. Em `__init__`, faz `self._plan_sha = ""`: nenhuma sessão de correção da review exige mudança, porque o histórico já tem push.
- Métodos privados: `_review_session(issue, pull, rounds) -> tuple[ReviewReport, str] | str` (relatório e sha clonado, ou a mensagem de falha da sessão, como em `_analysis_session`); `_review_fix_session(issue, round, findings) -> tuple[TestRunResult, str]` (testes + `fix_notes`; a falha do Claude vira vermelho com checkpoint de melhor esforço, como o `implement_fix`); `format_review_summary(round, report, follow_ups: dict[str, FollowUpIssue]) -> str` (função de módulo).

Fluxo do `run`, que é a parte que o implementador não deduz sozinho:

```
_move(MoveIssueRequest(id, to=In Review, from=In Review, require_states=(Done,)))  → False: OUTCOME_SKIPPED
rounds = []; fix_rounds = 0; follow_ups = []
loop:
  _still_in(issue, In Review) falso → OUTCOME_STOPPED
  pull = pull_request_info(identifier)  → None: OUTCOME_NO_PR
  fix_rounds >= MAX_REVIEW_ROUNDS → _ask(stuck, In Review, label curto); False → STOPPED; fix_rounds = 0
  r = _review_session(...)
  str (falhou) → rounds += ReviewRound(n, f"review falhou: {msg}"); fix_rounds += 1; continue
  report, reviewed_sha = r
  para cada achado "issue": create_issue(CreateIssueRequest(id, str(workflow.uuid4()), título, descrição com link da origem + detail + reason))
  post_comment(resumo da rodada)                  # id com workflow.uuid4()
  asks → _ask(issue, perguntas juntas, In Review); False → STOPPED; rounds += round; continue (não corrige antes da resposta)
  fixes → tests, notes = _review_fix_session(...)
          vermelho → tests = _fix_until_green(issue, tests, In Review); None → STOPPED
          rounds += ReviewRound(..., fix_notes=notes); fix_rounds += 1; continue
  rounds += round
  m = merge_pull_request(MergeRequest(identifier, title, pull.number, reviewed_sha))
  merged → _move(to=Done, from=In Review); return ReviewResult(ID, OUTCOME_MERGED, pull.url, follow_ups)
  head_changed → continue
  senão → _ask(f"A PR {pull.url} não pode ser mergeada: {m.reason}. ...", In Review); False → STOPPED; continue
```

- Timeouts: `pull_request_info`/`merge_pull_request`/`create_issue`/`post_comment` com 30 s e `maximum_attempts=5` (o merge com `mergeable=None` usa retry com `initial_interval` de 10 s). `review_pull` 30 min, `implement_review_fix` 35 min, ambos com heartbeat de 2 min e `maximum_attempts=1`.

- [ ] **Step 1: Escrever `tests/test_review_workflow.py`** com um `FakeReviewActivities` no estilo do fake do `test_implement_workflow.py`. Ele aceita como parâmetros uma lista de `ReviewReport` por rodada, a sequência de `MergeResult`, `red_runs`, `answers`, `stop_after_checks` e `pull_missing`. Testes:
  - `test_approved_review_merges_and_moves_to_done`: um relatório só com `note`/`reject` → chamadas `move_issue:In Review, move_issue:In Review, pull_request_info, create_sandbox, clone_branch, review_pull, kill_sandbox, post_comment, merge_pull_request, move_issue:Done`; resultado `ReviewResult("ENG-12", "merged", PR_URL)`; o merge recebe `head_sha` igual ao sha clonado; o primeiro move tem `require_states=("Done",)`.
  - `test_fix_findings_trigger_fix_session_and_rereview`: rodada 1 com um `fix` e rodada 2 aprovada → `implement_review_fix` recebe só o achado `fix`; commit `fix: review 1 de ENG-12`; o segundo `ReviewRequest.history` tem a rodada 1 com `fix_notes`.
  - `test_red_tests_after_review_fix_enter_fix_loop`: `red_runs=1` → `analyze_failure` e `implement_fix` rodam antes da segunda review; o checkpoint é `wip: review 1 de ENG-12 (testes vermelhos)`.
  - `test_issue_findings_create_follow_up_issues_once`: dois achados `issue` → dois `create_issue` com `new_issue_id` diferentes; `result.follow_ups` tem os dois identifiers; o resumo postado contém os dois links.
  - `test_ask_finding_waits_for_answer_and_rereviews_with_clarification`: um `ask` → `post_comment` do resumo, `post_comment` da pergunta, nenhum `implement_review_fix` antes da resposta; o `ReviewRequest.clarifications` seguinte tem a resposta.
  - `test_stuck_after_max_review_rounds_asks_human`: 3 rodadas com `fix` → pergunta de travamento antes da quarta review; a resposta zera o contador.
  - `test_moved_out_of_in_review_stops`: `stop_after_checks=1` → `"stopped"`, sem `merge_pull_request`.
  - `test_no_open_pull_request_is_no_pr`: `"no_pr"`, sem sandbox.
  - `test_merge_blocked_asks_human_then_retries`: merge `(False, reason="não mergeável (dirty)")` e depois `(True)` → a pergunta contém o motivo; depois da resposta há uma segunda review e o merge.
  - `test_head_changed_since_review_rereviews`: merge `head_changed=True` e depois `merged` → duas reviews, nenhuma pergunta.
  - `test_review_session_failure_counts_round_and_retries`: `review_pull` falha uma vez → segunda review e merge; o histórico tem `"review falhou:"`.
- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_review_workflow -v` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implementar `review_workflow.py`.**
- [ ] **Step 4: Rodar e ver passar** — mesmo comando → PASS; depois `PYTHONPATH=src python3 -m unittest discover -s tests` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: ReviewIssueWorkflow — review, correção em loop, follow-ups e merge"`

---

### Task 6: Webhook, roteamento das respostas, worker e docs

**Files:**
- Modify: `src/software_factory/linear/events.py`, `src/software_factory/linear/webhook.py`, `src/software_factory/orchestration/worker.py`
- Modify: `CLAUDE.md`, `README.md`
- Test: `tests/test_events.py`, `tests/test_webhook.py`

**Interfaces:**
- Consumes: `ReviewIssueWorkflow` (Task 5); activities das Tasks 2–4.
- Produces:
  - `events.py`: `_entered_state(payload, trigger_labels, state: str) -> bool` (o corpo atual de `should_implement`), com `should_implement` e o novo `should_review(payload, trigger_labels) -> bool` (`STATE_IN_REVIEW`) por cima dele.
  - `webhook.py`: ordem de despacho `should_process` > `should_implement` > `should_review` (`workflow_run=ReviewIssueWorkflow.run`, `id_prefix="review-issue"`). `handle_reply` manda o signal para `implement-issue-<id>` e para `review-issue-<id>`, ignorando `NOT_FOUND` em cada um. Devolve True se algum aceitou. Outro `RPCError` propaga (503).
  - `worker.py`: registra `ReviewIssueWorkflow` e as activities `review_pull`, `implement_review_fix`, `create_issue`, `pull_request_info`, `merge_pull_request`.

- [ ] **Step 1: Escrever os testes:** em `test_events.py`, `test_should_review_on_in_review_with_label` e `test_should_review_ignores_without_state_change`. Em `test_webhook.py`, `test_in_review_starts_review_workflow` (id `review-issue-issue-uuid`), `test_reply_signals_review_workflow_when_implement_not_found` (o primeiro handle levanta `NOT_FOUND` e o segundo aceita → `accepted`) e `test_reply_without_any_workflow_is_ignored` (ajusta o teste atual para os dois ids).
- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_events tests.test_webhook -v` → FAIL.
- [ ] **Step 3: Implementar** events, webhook e worker.
- [ ] **Step 4: Rodar tudo** — `PYTHONPATH=src python3 -m unittest discover -s tests` → PASS.
- [ ] **Step 5: Atualizar `CLAUDE.md`:**
  - na Stack, as skills `requesting-code-review` e `receiving-code-review` do superpowers 7.0.2 vendorizadas;
  - na Estrutura, `sessions.py`, `review_workflow.py`, `review_prompts.py` e as activities novas;
  - no Webhook do Linear, o gatilho `In Review`, o roteamento das respostas para os dois workflows e o fluxo da review (lentes, decisões, follow-ups sem label, merge squash com sha, `MAX_REVIEW_ROUNDS`, como parar, `Done`).

  Atualizar o `README.md`: o status `Done` no time, o token com merge (Contents + Pull requests: write, que já são necessários), branch protection que exige aprovação bloqueia o merge, e a sugestão de "auto-delete head branches".
- [ ] **Step 6: Commit** — `git commit -m "feat: webhook In Review dispara a review; respostas roteadas para implement e review"`

---

## Fora do escopo (registrar como issue se fizer falta)

- Comentar a review na PR do GitHub (hoje o resumo vai só no Linear).
- Atualizar a branch com a branch default (merge/rebase automático) quando há conflito. Hoje o workflow pergunta à pessoa.
- Lentes extras (performance, acessibilidade) ou uma lente por linguagem.
