# Reconciliação de issues perdidas com o servidor fora — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando o servidor de webhooks sobe, a factory varre o Linear e inicia os workflows que um webhook perdido teria iniciado: planejamento (`To Do`), implementação (`In Progress`) e review (`In Review`).

**Architecture:** No `lifespan`, o servidor de webhooks inicia um `ReconcileIssuesWorkflow` (id `reconcile-issues`). O workflow roda no worker, que é quem tem `LINEAR_API_KEY`. A activity `list_pending_issues` busca no Linear, via GraphQL, as issues com label de gatilho nos três status. Para cada uma, calcula o **momento do gatilho**: criação ou label adicionada, no caso de `To Do`, e entrada no status, no caso dos outros dois. A activity `start_missed_workflow` usa o client do Temporal para contar runs de `<prefixo>-<issue.id>` iniciados depois desse momento. Se não houver nenhum, inicia o workflow pelo mesmo caminho do webhook. A lógica de "qual workflow, qual id, como iniciar" sai do `webhook.py` para um módulo compartilhado.

**Tech Stack:** Python 3.12, temporalio 1.35 (`Client.count_workflows`, visibility query), httpx (GraphQL do Linear), FastAPI, unittest. Nenhuma dependência nova.

**Spec:** pedido do usuário em 2026-11-16: "hoje, apenas novas issues, adicionadas enquanto o servidor esta rodando sao executadas. Preciso que alem das novas issues adicionadas, issues que foram adicionadas enquanto o servidor estava fora sejam processadas." Respostas do usuário: cobrir **todas as etapas** (planejamento, implementação e review) e rodar **só no startup** do servidor de webhooks.

## Decisões tomadas neste plano (não estão na spec)

- **Quem varre:** um workflow do Temporal, não o servidor de webhooks direto. O servidor só tem `LINEAR_WEBHOOK_SECRET`, e as chaves do Linear ficam no worker. Assim também há retry e histórico visíveis na UI do Temporal. Se o worker estiver fora, o workflow espera na fila.
- **Critério de "evento perdido"**, igual para as três etapas: a issue tem label de gatilho, está no status da etapa e **nenhum run** de `<prefixo>-<issue.id>` começou a partir de `gatilho - CLOCK_SKEW_TOLERANCE`. Um run que falhou ou parou depois do gatilho conta como consumido. Isso repete a semântica do webhook: uma falha não é retentada até a pessoa mover a issue de novo. Um run ainda rodando cai em `WorkflowAlreadyStartedError` e é ignorado.
- **Momento do gatilho:**
  - `To Do` → planejamento: o maior entre `createdAt` da issue e o `createdAt` das entradas do `history` cujo `addedLabelIds` contém uma label de gatilho.
  - `In Progress` → implementação, `In Review` → review: o maior `createdAt` das entradas do `history` com `toState.name` igual ao status (case-insensitive). Se não houver nenhuma (issue criada direto no status), usa `createdAt`.
- **Só esses três status.** `In Planning` não entra: o `PlanIssueWorkflow` que falhou deixa a issue ali de propósito, sem compensação. Issue que ganhou a label fora de `To Do` com o servidor fora **não** é planejada. Fica registrado no README.
- **`CLOCK_SKEW_TOLERANCE = 2 min`:** o run iniciado pelo próprio webhook começa alguns segundos depois do `createdAt` do Linear. Com o relógio local atrasado, sem a tolerância, ele pareceria anterior ao gatilho e a issue seria reprocessada.
- **Limitação aceita:** a contagem depende do histórico do Temporal. Com `temporal server start-dev` sem `--db-filename` (em memória), ou depois da retenção do namespace, os runs antigos somem. Aí uma issue cujo run falhou volta a rodar no próximo startup. A persistência ainda não foi definida (CLAUDE.md), então o README só documenta isso e recomenda `--db-filename`.
- **Falha da reconciliação não derruba o servidor:** o erro ao iniciar o `ReconcileIssuesWorkflow` vai para o log e o servidor sobe mesmo assim.

## Global Constraints

- Nenhuma dependência nova. Linear via `LinearClient.execute` (GraphQL); Temporal via o `Client` que o worker já cria.
- Workflow determinístico: o `ReconcileIssuesWorkflow` só chama activities. `count_workflows`/`start_workflow` ficam na activity.
- Ids: `reconcile-issues` (reconciliação), `plan-issue-<id>`, `implement-issue-<id>`, `review-issue-<id>` (os mesmos de hoje).
- Kinds: `KIND_PLAN = "plan"`, `KIND_IMPLEMENT = "implement"`, `KIND_REVIEW = "review"`.
- `CLOCK_SKEW_TOLERANCE = timedelta(minutes=2)`; `ISSUES_PAGE_SIZE = 50`; `HISTORY_PAGE_SIZE = 50`.
- Horário na visibility query em RFC 3339 UTC sem fração: `%Y-%m-%dT%H:%M:%SZ`.
- Labels e status comparados case-insensitive, como no webhook (`STATE_TODO`, `STATE_IN_PROGRESS`, `STATE_IN_REVIEW` de `models.py`).
- Logs, mensagens e docs em português.

## Review Focus

1. **Run anterior falhou e a issue segue em `In Progress`:** reiniciar o servidor não pode disparar a implementação de novo (custo de sandbox e Claude). Teste em Task 3: `count_workflows` devolve 1 → não inicia.
2. **Webhook e reconciliação iniciam a mesma issue ao mesmo tempo:** só um workflow roda e a reconciliação segue com as outras issues. Teste em Task 3: `start_workflow` levanta `WorkflowAlreadyStartedError` → `False`, sem erro.
3. **Relógio local atrasado em relação ao Linear:** o run do próprio webhook tem que contar. Teste em Task 3: a query usa `gatilho - 2 min`.
4. **`history` fora de ordem** (a ordem padrão do Linear não está garantida aqui): o gatilho é o maior `createdAt`, não o primeiro nó. Teste em Task 2 com nós fora de ordem.
5. **Mais de 50 issues candidatas:** a paginação tem que trazer todas. Teste em Task 3 com duas páginas.

---

### Task 1: Extrair o início dos workflows de issue para `dispatch.py`

**Files:**
- Create: `src/software_factory/orchestration/dispatch.py`
- Modify: `src/software_factory/linear/webhook.py` (`handle_issue` e o despacho em `linear_webhook`)
- Test: `tests/test_dispatch.py`, `tests/test_webhook.py` (os existentes têm que seguir passando sem mudança)

**Interfaces:**
- Produces:
  - `KIND_PLAN`, `KIND_IMPLEMENT`, `KIND_REVIEW: str`
  - `ISSUE_WORKFLOWS: dict[str, tuple[Callable, str]]`: kind → (`PlanIssueWorkflow.run` / `ImplementIssueWorkflow.run` / `ReviewIssueWorkflow.run`, prefixo `"plan-issue"` / `"implement-issue"` / `"review-issue"`)
  - `issue_workflow_id(kind: str, issue_id: str) -> str` → `f"{prefix}-{issue_id}"`
  - `async start_issue_workflow(client: Any, task_queue: str, kind: str, issue: PlanIssueInput, context: str = "") -> bool`: `False` em `WorkflowAlreadyStartedError`; outros erros propagam. `context` só entra no log (o webhook passa `delivery=<id>`).
  - `webhook.handle_issue(client, task_queue, issue: LinearIssue, delivery_id, kind: str = KIND_PLAN) -> bool`: converte `LinearIssue` em `PlanIssueInput` e delega. Os kwargs `workflow_run`/`id_prefix` saem e `linear_webhook` passa `kind=`.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_dispatch.py` (`IsolatedAsyncioTestCase`, com um fake de client igual ao `FakeTemporalClient` de `test_webhook.py`):
  - `test_starts_each_kind`: para cada kind, `args[0]` é o `run` esperado, `kwargs["id"] == "<prefixo>-issue-1"`, `kwargs["task_queue"] == "q"`, `args[1]` é o `PlanIssueInput` e o retorno é `True`.
  - `test_already_started_returns_false`: o client levanta `WorkflowAlreadyStartedError("id", "type")` → `False`.
  - `test_other_errors_propagate`: `RuntimeError` → levanta.
- [ ] **Step 2: Rodar e ver falhar:** `PYTHONPATH=src python3 -m unittest tests.test_dispatch -v`. Esperado: `ModuleNotFoundError`.
- [ ] **Step 3: Implementar `dispatch.py` e adaptar `webhook.py`** como no bloco Interfaces. O `workflow_kwargs` vira `kind`.
- [ ] **Step 4: Rodar a suíte:** `PYTHONPATH=src python3 -m unittest discover -s tests`. Esperado: tudo verde, incluindo `test_webhook` sem alteração.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/dispatch.py src/software_factory/linear/webhook.py tests/test_dispatch.py
git commit -m "refactor: início dos workflows de issue em dispatch.py"
```

### Task 2: Regras puras da reconciliação

**Files:**
- Create: `src/software_factory/orchestration/reconcile.py`
- Modify: `src/software_factory/orchestration/models.py` (dataclasses novas)
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `KIND_*` (Task 1); `STATE_TODO`, `STATE_IN_PROGRESS`, `STATE_IN_REVIEW`, `PlanIssueInput` (models).
- Produces (em `models.py`):
  - `ReconcileInput(trigger_labels: tuple[str, ...])`
  - `PendingIssue(kind: str, issue: PlanIssueInput, triggered_at: str)` (`triggered_at` em ISO 8601 UTC, como vem do Linear)
  - `ReconcileResult(started: list[str], skipped: int)` (`started` = workflow ids)
- Produces (em `reconcile.py`):
  - `KIND_BY_STATE: dict[str, str]`: `"to do"` → `KIND_PLAN`, `"in progress"` → `KIND_IMPLEMENT`, `"in review"` → `KIND_REVIEW` (chaves em minúsculas, derivadas das constantes `STATE_*`)
  - `pending_issue(node: dict[str, Any], trigger_labels: Iterable[str]) -> PendingIssue | None`. `node` tem o formato da query da Task 3: `id identifier title description url createdAt state{name} labels{nodes{id name}} history{nodes{createdAt addedLabelIds toState{name}}}`. Devolve `None` sem label de gatilho ou com status fora de `KIND_BY_STATE`.
  - `trigger_time(node, kind, trigger_label_ids: set[str]) -> str`: as regras de "Momento do gatilho" acima. Compara os horários como `datetime` (`datetime.fromisoformat`) e devolve a string original do nó vencedor.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_reconcile.py`, com um helper `node(state, labels, created_at, history)`:
  - `test_todo_with_label_is_plan`: `"To Do"` + `Feature` → `kind == "plan"`, `issue.identifier == "ENG-12"`, `triggered_at == created_at`.
  - `test_label_case_insensitive`: label `"bug"`, trigger `("Feature", "Bug")` → não é `None`.
  - `test_no_trigger_label_is_none` e `test_in_planning_is_none` → `None`.
  - `test_plan_trigger_is_latest_label_add`: histórico com `addedLabelIds=["label-feature"]` em `2026-11-15T10:00:00.000Z` e criação em `2026-11-14...` → `triggered_at == "2026-11-15T10:00:00.000Z"`. Uma entrada que adiciona só `label-docs` mais tarde é ignorada.
  - `test_in_progress_trigger_is_latest_entry_into_state` (**Review Focus 4**): nós na ordem `[10:00 → In Progress, 12:00 → In Progress, 11:00 → In Review]`, status atual `In Progress` → `kind == "implement"`, `triggered_at` é o de 12:00.
  - `test_in_review_without_history_uses_created_at` → `kind == "review"`, `triggered_at == created_at`.
- [ ] **Step 2: Rodar e ver falhar:** `PYTHONPATH=src python3 -m unittest tests.test_reconcile -v`. Esperado: `ModuleNotFoundError`.
- [ ] **Step 3: Implementar** os dataclasses e `reconcile.py` como no bloco Interfaces.
- [ ] **Step 4: Rodar:** o mesmo comando. Esperado: PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/reconcile.py src/software_factory/orchestration/models.py tests/test_reconcile.py
git commit -m "feat: regras da reconciliação de issues perdidas"
```

### Task 3: Activities da reconciliação

**Files:**
- Create: `src/software_factory/orchestration/reconcile_activities.py`
- Test: `tests/test_reconcile_activities.py`

**Interfaces:**
- Consumes: `LinearClient.execute`; `pending_issue` (Task 2); `issue_workflow_id`, `start_issue_workflow` (Task 1).
- Produces: `ReconcileActivities(linear: LinearClient, temporal: Any, task_queue: str)` com:
  - `@activity.defn async list_pending_issues(self, request: ReconcileInput) -> list[PendingIssue]`: pagina `PENDING_ISSUES_QUERY` (`first: ISSUES_PAGE_SIZE`, `after: $after`, até `pageInfo.hasNextPage == false`) e aplica `pending_issue` em cada nó. Filtro da query: `or` de `state: {name: {eqIgnoreCase: ...}}` para os três status. As labels são filtradas no Python. `history(first: HISTORY_PAGE_SIZE)` com os campos da Task 2.
  - `@activity.defn async start_missed_workflow(self, pending: PendingIssue) -> bool`: monta `since = parse(triggered_at) - CLOCK_SKEW_TOLERANCE` em UTC. Se `(await temporal.count_workflows(f"WorkflowId = '{wid}' AND StartTime >= '{since:%Y-%m-%dT%H:%M:%SZ}'")).count > 0`, devolve `False`. Senão devolve `await start_issue_workflow(temporal, task_queue, pending.kind, pending.issue, "reconciliação")`. Loga o motivo de cada `False`.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_reconcile_activities.py`. O Linear vem de `httpx.MockTransport` (como `test_linear_client.py`). O Temporal é um fake com `count_workflows(query)`, que registra a query e devolve um objeto com `.count`, e `start_workflow`, que registra a chamada e levanta um erro configurável.
  - `test_lists_all_pages` (**Review Focus 5**): página 1 com `hasNextPage: true, endCursor: "c1"` e uma issue `To Do`/`Feature`; página 2 com uma `In Review`/`Bug` e uma sem label. Resultado: 2 `PendingIssue`. A 2ª requisição leva `variables["after"] == "c1"`.
  - `test_skips_when_run_after_trigger` (**Review Focus 1**): `count = 1` → `False` e nenhum `start_workflow`.
  - `test_query_uses_skew_tolerance` (**Review Focus 3**): `triggered_at = "2026-11-16T10:00:00.000Z"`, kind `implement`, `issue_id = "issue-1"` → query exatamente `"WorkflowId = 'implement-issue-issue-1' AND StartTime >= '2026-11-16T09:58:00Z'"`.
  - `test_starts_when_no_run`: `count = 0` → `True`, `kwargs["id"] == "implement-issue-issue-1"`, `kwargs["task_queue"] == "q"`.
  - `test_running_workflow_is_skipped` (**Review Focus 2**): `count = 0` e `start_workflow` levanta `WorkflowAlreadyStartedError` → `False`.
- [ ] **Step 2: Rodar e ver falhar:** `PYTHONPATH=src python3 -m unittest tests.test_reconcile_activities -v`. Esperado: `ModuleNotFoundError`.
- [ ] **Step 3: Implementar** `reconcile_activities.py` como no bloco Interfaces.
- [ ] **Step 4: Rodar:** o mesmo comando. Esperado: PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/reconcile_activities.py tests/test_reconcile_activities.py
git commit -m "feat: activities que listam issues pendentes e iniciam o workflow perdido"
```

### Task 4: `ReconcileIssuesWorkflow` e registro no worker

**Files:**
- Create: `src/software_factory/orchestration/reconcile_workflow.py`
- Modify: `src/software_factory/orchestration/worker.py`
- Test: `tests/test_reconcile_workflow.py`

**Interfaces:**
- Consumes: `ReconcileInput`, `PendingIssue`, `ReconcileResult` (Task 2); activities `list_pending_issues` e `start_missed_workflow` (Task 3), chamadas por nome.
- Produces: `@workflow.defn ReconcileIssuesWorkflow.run(self, request: ReconcileInput) -> ReconcileResult`, com `RECONCILE_WORKFLOW_ID = "reconcile-issues"` no mesmo módulo.
  - `list_pending_issues`: `start_to_close_timeout=2 min`, `RetryPolicy(maximum_attempts=5)`.
  - `start_missed_workflow`, uma issue por vez, na ordem da lista: `start_to_close_timeout=1 min`, `RetryPolicy(maximum_attempts=5)`. `True` acrescenta `issue_workflow_id(kind, issue_id)` a `started`; `False` incrementa `skipped`.
  - Uma issue cujo `start_missed_workflow` esgota os retries faz o workflow falhar (o erro fica visível na UI). Não há compensação.
- Worker: `ReconcileActivities(LinearClient(settings.linear_api_key), client, settings.task_queue)`; registra `ReconcileIssuesWorkflow` e as duas activities.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_reconcile_workflow.py`, com `WorkflowEnvironment.start_time_skipping()` e activities fake por nome, no padrão de `tests/test_workflow.py`:
  - `test_starts_missed_and_counts_skipped`: 3 pendentes, e o fake de `start_missed_workflow` devolve `[True, False, True]` → `started == ["plan-issue-a", "review-issue-c"]`, `skipped == 1`, e o input de `list_pending_issues` traz os `trigger_labels` passados.
  - `test_no_pending`: lista vazia → `ReconcileResult(started=[], skipped=0)`.
- [ ] **Step 2: Rodar e ver falhar:** `PYTHONPATH=src python3 -m unittest tests.test_reconcile_workflow -v`. Esperado: `ModuleNotFoundError`.
- [ ] **Step 3: Implementar** o workflow, com os imports de models/dispatch em `workflow.unsafe.imports_passed_through()`, e o registro no worker.
- [ ] **Step 4: Rodar a suíte:** `PYTHONPATH=src python3 -m unittest discover -s tests`. Esperado: PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/reconcile_workflow.py src/software_factory/orchestration/worker.py tests/test_reconcile_workflow.py
git commit -m "feat: ReconcileIssuesWorkflow registrado no worker"
```

### Task 5: Disparo no startup do servidor e docs

**Files:**
- Modify: `src/software_factory/api.py` (`lifespan`)
- Modify: `README.md`, `CLAUDE.md`
- Test: `tests/test_api_startup.py`

**Interfaces:**
- Consumes: `ReconcileIssuesWorkflow.run`, `RECONCILE_WORKFLOW_ID` (Task 4); `ReconcileInput` (Task 2); `Settings.trigger_labels`, `Settings.task_queue`.
- Produces: `async start_reconcile(client: Any, settings: Settings) -> None` em `api.py`. Chama `client.start_workflow(ReconcileIssuesWorkflow.run, ReconcileInput(tuple(sorted(settings.trigger_labels))), id=RECONCILE_WORKFLOW_ID, task_queue=settings.task_queue)`. `WorkflowAlreadyStartedError` vira log info; qualquer outra exceção vira `logger.exception`, sem propagar. O `lifespan` chama `start_reconcile` depois de garantir `app.state.temporal`, antes do `yield`.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_api_startup.py`, usando `with TestClient(create_app(Settings(linear_webhook_secret="s"), temporal_client=fake))`, que roda o lifespan:
  - `test_startup_starts_reconcile`: `fake.started` tem um item com `args[0] is ReconcileIssuesWorkflow.run`, `kwargs["id"] == "reconcile-issues"` e `args[1].trigger_labels == ("bug", "feature")`.
  - `test_startup_survives_already_started` e `test_startup_survives_temporal_error` (`RuntimeError`): `/health` responde 200.
- [ ] **Step 2: Rodar e ver falhar:** `PYTHONPATH=src python3 -m unittest tests.test_api_startup -v`. Esperado: FAIL (nada iniciado).
- [ ] **Step 3: Implementar `start_reconcile`** e a chamada no `lifespan`.
- [ ] **Step 4: Rodar a suíte:** `PYTHONPATH=src python3 -m unittest discover -s tests`. Esperado: PASS. Os testes antigos de `test_webhook` usam `TestClient` sem `with` e não rodam o lifespan.
- [ ] **Step 5: Atualizar docs.**
  - **README:** uma seção "Reconciliação no startup" com o que é varrido, o critério do gatilho e a tolerância de 2 min. Diz que label adicionada fora de `To Do` com o servidor fora não é recuperada e recomenda `temporal server start-dev --db-filename <arquivo>`, explicando a limitação do histórico em memória e da retenção.
  - **CLAUDE.md:** troca "Não há varredura de issues existentes em TODO" por um resumo da reconciliação. Acrescenta `dispatch.py`, `reconcile.py`, `reconcile_activities.py` e `reconcile_workflow.py` na Estrutura.
- [ ] **Step 6: Commit**

```bash
git add src/software_factory/api.py tests/test_api_startup.py README.md CLAUDE.md
git commit -m "feat: servidor de webhooks inicia a reconciliação no startup"
```
