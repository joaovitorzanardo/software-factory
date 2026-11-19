# Relatório de tokens por etapa — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cada sessão do Claude que a factory roda grava os tokens gastos (input, output, escrita e leitura de cache), o custo estimado e quanto da janela de 5h da assinatura consumiu, num SQLite do worker. Ao fim de cada etapa (planejamento, implementação, review), um único comentário da issue no Linear é criado ou editado com o acumulado por etapa. Ao mover para `Done`, o comentário ganha a linha `Total:`.

**Architecture:** `_run_claude` passa a ler todo stream-json com um `TokenMeter` puro (eventos `result`, `assistant` e `rate_limit_event`) e, no `finally`, grava um registro por sessão no `TokenStore` (SQLite, `sqlite3` da stdlib). A etapa e a issue saem do id do workflow (`plan-issue-<id>`, `implement-issue-<id>`, `review-issue-<id>`), então nenhum request de activity muda. O comentário é sempre **regenerado do banco**: a activity `token_usage_report` monta o texto e a activity `upsert_comment` cria ou edita o comentário de id fixo derivado da issue. Os três workflows chegam no mesmo comentário sem guardar estado, e o que uma etapa que falhou gastou aparece na próxima publicação.

**Tech Stack:** Python 3.12, `sqlite3`, `uuid`, temporalio, Linear GraphQL (`commentUpdate`), unittest. Nenhuma dependência de pacote nova.

**Spec:** pedido do usuário em 2026-11-18 (nesta conversa). Comentário `Planejamento: <tokens>` ao fim do planejamento, editado para `Planejamento: … Implementação: …` ao fim da implementação, e assim por diante; ao passar para Done, o total do processo. Respostas do usuário: guardar em **SQLite**; etapa que roda de novo **acumula**; total no **mesmo comentário**.

## Decisões tomadas neste plano (não estão na spec)

- **Tokens guardados separados**: `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, mais `cost_usd` (o `total_cost_usd` do `result`; com assinatura é estimativa) e `model`. O total exibido é a soma dos quatro e é calculado, nunca guardado.
- **Fonte dos tokens**: o `usage` do evento `result`, que soma a sessão inteira. Se a sessão morrer sem `result` (timeout, erro), usa a soma dos `message.usage` dos eventos `assistant`, deduplicados por `message.id` (fica o último visto de cada id). O registro sai marcado `complete = 0` e a linha do comentário mostra `(parcial)`.
- **% da sessão** (só com `CLAUDE_CODE_OAUTH_TOKEN`): por sessão, guarda para cada `resetsAt` da janela `five_hour` o primeiro e o último `utilization` vistos. A fatia de um grupo de sessões é `Σ por resetsAt de (max(último) − min(primeiro))`. Isso cobre sessões em paralelo (lentes da review) sem contar duas vezes, e cobre a virada de janela no meio da etapa. Limitação aceita: uso da mesma conta fora da factory, no mesmo intervalo, entra na conta. Sem eventos (API key), a fatia é `None` e o comentário a omite.
- **Etapa** = prefixo do id do workflow. As correções feitas dentro da review contam como `Review`; análise e correções do loop vermelho da implementação contam como `Implementação`. O `run_tests` não usa Claude e não entra.
- **Chave do registro**: `uuid4` por sessão. Retry de activity roda uma sessão nova, que gasta tokens de verdade, então dois registros estão certos. Colunas `workflow_id`, `run_id`, `activity`, `attempt` e `recorded_at` ficam para análise.
- **Gravação nunca derruba a sessão**: erro no meter ou no SQLite só vai para o log, como no aviso de uso.
- **Publicação nunca derruba o workflow**: `publish_token_usage` engole `ActivityError` com warning.
- **Quando publica**: plano commitado (antes de mover para In Progress); PR aberta (antes de mover para In Review); implementação parada (`OUTCOME_STOPPED`); merge (depois de mover para Done, com `final=True`); review parada. Workflow que falha não publica; os registros ficam no banco e aparecem na próxima publicação.
- **Id do comentário**: UUID **v4 no formato**, com os bits vindos de `sha256("token-usage:" + issue_id)` (`uuid.UUID(bytes=digest[:16], version=4)`). É determinístico como um uuid5, e é o mesmo formato v4 que `post_comment` já manda ao Linear hoje, então não depende de o Linear aceitar outra versão.
- **Banco**: `TOKEN_USAGE_DB` (env do worker, default `token_usage.db` no diretório corrente), no `.gitignore`. Uma conexão por operação, via `asyncio.to_thread`. Supõe **um worker só** escrevendo no arquivo, que é a realidade da fase de teste.
- Edição do comentário dispara webhook `Comment` `update`, e criação sem `parentId` é ignorada por `handle_reply`. Nenhum dos dois entra no fluxo de respostas.

## Resultado do spike (Task 1, 2026-11-18, Claude Code com assinatura)

- `rate_limit_event` sai **depois de cada resposta da API** (2 eventos numa sessão de 2 turnos: 0.61 → 0.62). A diferença dentro da sessão funciona. A granularidade é 0.01, então etapas muito curtas mostram 0% ou 1%.
- `result.usage` traz `input_tokens`, `output_tokens`, `cache_creation_input_tokens` e `cache_read_input_tokens`, somando a sessão inteira. `total_cost_usd` também vem com assinatura.
- Os eventos `assistant` repetem o `message.id`, um por bloco de conteúdo, e o `output_tokens` deles é um retrato parcial do streaming (4 contra 175 no `result`). O fallback sem `result` **subestima o output**, e por isso o registro sai marcado como parcial.
- O `init` traz `model`.
- O uso de UUID v5 como id de comentário no Linear não foi testado: a decisão acima usa o formato v4.



- Nenhuma dependência de pacote nova (`sqlite3`, `uuid`, `asyncio.to_thread` da stdlib).
- Workflows determinísticos: SQLite e Linear só em activities; o id do comentário (hash puro) pode ser calculado no workflow.
- Constantes: `STAGE_PLAN = "plan"`, `STAGE_IMPLEMENT = "implement"`, `STAGE_REVIEW = "review"`; rótulos `Planejamento`, `Implementação`, `Review`, `Total`; ordem de exibição plan → implement → review.
- Env nova do worker: `TOKEN_USAGE_DB` (default `token_usage.db`).
- Números em pt-BR: milhar com `.` (`1.234.567`), percentual com uma casa e vírgula (`3,2%`), custo `US$ 0,84`.
- Logs, comentário e docs em português.
- Testes: `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Trabalhar numa branch nova a partir de `main`: `feat/token-usage-report`.

## Review Focus

1. **Sessão que morre por timeout no meio** (sem `result`): os tokens já gastos têm de entrar, marcados como parciais. Teste na Task 2 (`test_falls_back_to_assistant_usage`) e na Task 5 (`test_records_usage_when_session_times_out`).
2. **Mesmo `message.id` repetido no stream** (um evento `assistant` por bloco de conteúdo): contar uma vez só. Teste na Task 2 (`test_dedupes_assistant_by_message_id`).
3. **Lentes da review em paralelo na mesma janela**: o % da etapa não pode dobrar. Teste na Task 2 (`test_session_share_overlapping_sessions`).
4. **Linear fora na hora de publicar**: o workflow segue e a issue ainda vai para In Review/Done. Teste na Task 8 (`test_publish_failure_does_not_fail_workflow`).
5. **Issue sem nenhuma sessão gravada** (worker sem banco, API key sem eventos): o comentário não quebra e não mostra `%`. Teste na Task 4 (`test_render_without_share_or_cost`).

---

### Task 1: Spike — conferir o stream-json

Não gera código de produção. Confirma três fatos antes de construir em cima deles.

**Files:**
- Create: `tests/stream_samples.py` (linhas reais do stream, encurtadas e sem nada sensível)

- [ ] **Step 1: Capturar uma sessão real com assinatura**

Run (local, com `CLAUDE_CODE_OAUTH_TOKEN` exportado): `claude -p "liste os arquivos e leia o README" --output-format stream-json --verbose > /tmp/claude-stream.jsonl`

- [ ] **Step 2: Conferir os campos**

Run: `jq -c 'select(.type=="result") | {usage, total_cost_usd}' /tmp/claude-stream.jsonl; jq -c 'select(.type=="assistant") | {id: .message.id, usage: .message.usage}' /tmp/claude-stream.jsonl; jq -c 'select(.type=="rate_limit_event")' /tmp/claude-stream.jsonl | wc -l; jq -c 'select(.type=="system" and .subtype=="init") | .model' /tmp/claude-stream.jsonl`

Expected: `result.usage` tem `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`; `assistant` tem `message.id` e `message.usage`; o `init` tem `model`. **Anote quantos `rate_limit_event` saem.** Se for só um por sessão, `first == last` e a fatia de uma sessão isolada dá 0: registre isso no plano e no CLAUDE.md como limitação (o % passa a ser medido entre sessões consecutivas da mesma etapa, o que a fórmula `max(último) − min(primeiro)` já faz quando a etapa tem mais de uma sessão).

- [ ] **Step 3: Salvar amostras em `tests/stream_samples.py`**

Constantes `RESULT_EVENT`, `ASSISTANT_EVENT`, `RATE_LIMIT_EVENT`, `INIT_EVENT` (dicts), cópias reais encurtadas.

- [ ] **Step 4: Commit**

```bash
git add tests/stream_samples.py docs/superpowers/plans/2026-11-18-token-usage-report.md
git commit -m "test: amostras reais do stream-json para a contagem de tokens"
```

### Task 2: `TokenMeter` e fatia da sessão (puro)

**Files:**
- Create: `src/software_factory/orchestration/token_usage.py`
- Test: `tests/test_token_usage.py`

**Interfaces:**
- Produces:
  - `TokenCounts(input_tokens: int = 0, output_tokens: int = 0, cache_creation_tokens: int = 0, cache_read_tokens: int = 0)`, frozen, com `total -> int` e `__add__`.
  - `WindowSample(resets_at: int, first: float, last: float)`, frozen.
  - `SessionUsage(tokens: TokenCounts, cost_usd: float | None, model: str | None, complete: bool, windows: tuple[WindowSample, ...])`, frozen.
  - `class TokenMeter: feed(chunk: str) -> None; usage() -> SessionUsage | None` (`None` se não viu tokens nem janela).
  - `session_share(samples: Iterable[WindowSample]) -> float | None`.
  - `STAGE_PLAN`, `STAGE_IMPLEMENT`, `STAGE_REVIEW`, `STAGE_LABELS: dict[str, str]` (ordem de exibição), `stage_for_workflow(workflow_id: str) -> tuple[str, str] | None` → `(stage, issue_id)`.
  - `token_usage_comment_id(issue_id: str) -> str`.

- [ ] **Step 1: Escrever os testes que falham**

Use as amostras de `tests/stream_samples.py`. Testes:
- `test_uses_result_usage`: stream com `assistant` e `result`; `usage().tokens` igual ao `result.usage`, `complete is True`, `cost_usd == result.total_cost_usd`, `model` do `init`.
- `test_falls_back_to_assistant_usage`: dois `assistant` com ids distintos e sem `result`; tokens = soma; `complete is False`; `cost_usd is None`.
- `test_dedupes_assistant_by_message_id`: o mesmo id duas vezes (o segundo com `output_tokens` maior); conta só o último.
- `test_line_split_across_chunks`: o `result` dividido em dois `feed`.
- `test_tracks_first_and_last_utilization_per_window`: três `rate_limit_event` (0.30, 0.35 com `resetsAt` A; 0.02 com `resetsAt` B) → `windows == (WindowSample(A, 0.30, 0.35), WindowSample(B, 0.02, 0.02))`.
- `test_ignores_garbage_lines`: linha não JSON e JSON que não é dict não levantam.
- `test_usage_none_without_data`.
- `test_session_share_overlapping_sessions`: `[WindowSample(A, .30, .40), WindowSample(A, .32, .45)]` → `0.15` (`assertAlmostEqual`).
- `test_session_share_across_windows`: `[WindowSample(A, .90, .98), WindowSample(B, .00, .05)]` → `0.13`.
- `test_session_share_none_without_samples`.
- `test_stage_for_workflow`: `"implement-issue-abc"` → `("implement", "abc")`; `"usage-alert-five-hour-1"` → `None`.
- `test_comment_id_is_stable`: mesma issue → mesmo id; issues diferentes → ids diferentes; `uuid.UUID(id).version == 4`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_token_usage -v`
Expected: FAIL com `ModuleNotFoundError: software_factory.orchestration.token_usage`

- [ ] **Step 3: Implementar `token_usage.py`**

O `TokenMeter` remonta linhas como o `UsageWatcher` (`usage.py`). A janela vem de `rate_limit_info.unifiedWindows.five_hour`, com fallback no topo quando `rateLimitType == "five_hour"`: extraia isso de `session_usage` para um helper `five_hour_window(event) -> tuple[float, int] | None` em `usage.py` e use nos dois lugares (o `test_usage.py` existente continua passando). Validar números com o `_number` de `usage.py`. `stage_for_workflow` usa `{"plan-issue-": STAGE_PLAN, "implement-issue-": STAGE_IMPLEMENT, "review-issue-": STAGE_REVIEW}`. `token_usage_comment_id` segue a decisão do id do comentário (v4 a partir do sha256).

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_token_usage tests.test_usage -v`
Expected: PASS

- [ ] **Step 5: Teste de deriva dos prefixos**

Em `tests/test_dispatch.py`, `test_token_stage_prefixes_match_workflows`: para cada `kind` de `ISSUE_WORKFLOWS`, `stage_for_workflow(issue_workflow_id(kind, "x"))[1] == "x"` e o stage é `kind`. Run e ver passar.

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/orchestration/token_usage.py src/software_factory/orchestration/usage.py tests/test_token_usage.py tests/test_dispatch.py
git commit -m "feat: contagem de tokens e fatia da sessão no stream-json"
```

### Task 3: `TokenStore` (SQLite) e config

**Files:**
- Create: `src/software_factory/orchestration/token_store.py`
- Modify: `src/software_factory/orchestration/token_usage.py` (`StageUsage`, `IssueUsage`)
- Modify: `src/software_factory/config.py` (campo `token_usage_db: str = "token_usage.db"`, lido de `TOKEN_USAGE_DB`)
- Modify: `.gitignore` (`token_usage.db`)
- Test: `tests/test_token_store.py`

**Interfaces:**
- Consumes: `SessionUsage`, `TokenCounts`, `WindowSample`, `session_share`, `STAGE_LABELS` (Task 2).
- Produces:
  - `SessionRecord(key: str, issue_id: str, stage: str, workflow_id: str, run_id: str, activity: str, attempt: int, recorded_at: str, usage: SessionUsage)`, frozen.
  - Em `token_usage.py` (o store importa de lá; a Task 4 usa sem import circular): `StageUsage(tokens: TokenCounts, cost_usd: float | None, share: float | None, sessions: int, partial: bool)`, frozen; `IssueUsage(stages: dict[str, StageUsage], total: StageUsage | None)`: `stages` só com etapas que têm sessão, na ordem de `STAGE_LABELS`; `total` é `None` sem sessões.
  - `class TokenStore(path: str)`: `record(record: SessionRecord) -> None`, `issue_usage(issue_id: str) -> IssueUsage` (síncronos; quem chama usa `asyncio.to_thread`).

Esquema (criado no `__init__` com `CREATE TABLE IF NOT EXISTS`):

```sql
sessions(key TEXT PRIMARY KEY, issue_id TEXT NOT NULL, stage TEXT NOT NULL,
         workflow_id TEXT, run_id TEXT, activity TEXT, attempt INTEGER, recorded_at TEXT,
         model TEXT, complete INTEGER, input_tokens INTEGER, output_tokens INTEGER,
         cache_creation_tokens INTEGER, cache_read_tokens INTEGER, cost_usd REAL)
session_windows(session_key TEXT, resets_at INTEGER, util_first REAL, util_last REAL,
                PRIMARY KEY (session_key, resets_at))
CREATE INDEX IF NOT EXISTS sessions_issue ON sessions(issue_id)
```

- [ ] **Step 1: Escrever os testes que falham** (banco em `tempfile.TemporaryDirectory`)
- `test_accumulates_sessions_per_stage`: duas sessões `implement` e uma `plan` → `stages["implement"].tokens` é a soma das duas, `sessions == 2`; ordem das chaves `["plan", "implement"]`.
- `test_rerun_accumulates`: duas sessões `plan` de `run_id` diferentes → somadas.
- `test_cost_none_when_no_session_has_cost` e `test_cost_sums_known_costs`.
- `test_partial_flag`: uma sessão com `complete=False` → `partial is True`.
- `test_share_from_windows`: janelas de duas sessões da etapa → `share` igual a `session_share` delas; `total.share` calculado sobre todas as sessões da issue.
- `test_record_is_idempotent_by_key`: gravar a mesma chave duas vezes → `sessions == 1` (`INSERT OR IGNORE`).
- `test_empty_issue`: `IssueUsage(stages={}, total=None)`.
- `test_persists_across_instances`: grava com uma instância e lê com outra no mesmo arquivo.
- Em `tests/test_config.py` (ou no teste de settings existente): `TOKEN_USAGE_DB` lido; default `token_usage.db`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_token_store -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementar `TokenStore` e o campo de config**

`sqlite3.connect(path)` por operação dentro de `with closing(...)`, e `with conn:` para o commit.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_token_store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/token_store.py src/software_factory/orchestration/token_usage.py src/software_factory/config.py .gitignore tests/test_token_store.py
git commit -m "feat: SQLite com o uso de tokens por sessão do Claude"
```

### Task 4: Texto do comentário

**Files:**
- Modify: `src/software_factory/orchestration/token_usage.py`
- Test: `tests/test_token_usage.py`

**Interfaces:**
- Consumes: `IssueUsage`, `StageUsage` (definidos em `token_usage.py` na Task 3).
- Produces: `render_usage_comment(usage: IssueUsage, final: bool) -> str`.

Formato exato (uma linha por etapa presente; `% da sessão` e custo só quando não `None`; `(parcial)` quando `partial`):

```
**Tokens gastos pela factory**

Planejamento: 1.234.567 tokens · 3,2% da sessão · US$ 0,84
Implementação: 4.800.000 tokens (parcial) · 14,0% da sessão · US$ 3,10
Total: 6.034.567 tokens · 17,2% da sessão · US$ 3,94

+++ Detalhes

| Etapa | Sessões | Input | Cache (escrita) | Cache (leitura) | Output | Custo |
|---|---|---|---|---|---|---|
| Planejamento | 1 | 45.000 | 60.000 | 1.111.567 | 18.000 | US$ 0,84 |
| … |

+++
```

A linha `Total:` (e a linha `Total` da tabela) só aparece com `final=True`. Sem nenhuma etapa: `**Tokens gastos pela factory**\n\nNenhuma sessão do Claude registrada.`

- [ ] **Step 1: Escrever os testes que falham**
- `test_render_stages_in_order` (`final=False`): contém `Planejamento: 1.234.567 tokens · 3,2% da sessão · US$ 0,84` e a linha de Implementação depois; não contém `Total:`.
- `test_render_final_adds_total`: com `final=True`, contém `Total: 6.034.567 tokens`.
- `test_render_without_share_or_cost`: `share=None`, `cost_usd=None` → `Planejamento: 1.000 tokens` sem `·`.
- `test_render_partial`.
- `test_render_empty`.

- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_token_usage -v`, FAIL em `render_usage_comment`.
- [ ] **Step 3: Implementar `render_usage_comment`**
- [ ] **Step 4: Rodar e ver passar** — mesmo comando, PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/token_usage.py tests/test_token_usage.py
git commit -m "feat: texto do comentário de tokens por etapa"
```

### Task 5: Gravar cada sessão em `_run_claude`

**Files:**
- Modify: `src/software_factory/orchestration/activities.py:280-320` (`SandboxActivities.__init__` e `_run_claude`)
- Modify: `src/software_factory/orchestration/worker.py` (`TokenStore(settings.token_usage_db)` passado ao `SandboxActivities`)
- Test: `tests/test_activities.py`

**Interfaces:**
- Consumes: `TokenMeter`, `stage_for_workflow` (Task 2); `TokenStore.record`, `SessionRecord` (Task 3).
- Produces: `SandboxActivities(settings, sandbox_cls=AsyncSandbox, usage_notifier=None, token_store: TokenStore | None = None)`.

- [ ] **Step 1: Escrever os testes que falham** (no estilo dos testes de `_run_claude` existentes, sandbox fake emitindo chunks pelo `on_stdout`, `TokenStore` num diretório temporário)
- `test_records_session_usage`: `implement_plan` num workflow `implement-issue-abc` → `store.issue_usage("abc").stages["implement"].tokens` igual ao `result` emitido.
- `test_records_usage_when_session_times_out`: o fake emite um `assistant` e levanta `TimeoutException`; a activity levanta, e o registro existe com `partial is True`.
- `test_store_error_does_not_fail_session`: `record` levanta `sqlite3.OperationalError`; `implement_plan` devolve o texto normal.
- `test_no_record_outside_issue_workflow`: workflow id `outro-x` → nada gravado, sem erro.
- `test_usage_alert_still_fires`: o teste de aviso existente continua passando com o store ligado.

- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_activities -v`
- [ ] **Step 3: Implementar**

`_run_claude` passa sempre um `on_line` que alimenta o `TokenMeter` (e o `UsageWatcher` quando há notifier), e no `finally` grava via `asyncio.to_thread(self.token_store.record, …)` dentro de `try/except Exception` com `activity.logger.warning`. Campos do registro a partir do `activity.info()` (`workflow_id`, `workflow_run_id`, `activity_type`, `attempt`); `recorded_at` em UTC ISO 8601; `key = uuid.uuid4().hex`. Sem `token_store`, sem `usage()` ou sem etapa: não grava.

- [ ] **Step 4: Rodar e ver passar** — `PYTHONPATH=src python3 -m unittest discover -s tests`, tudo PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/activities.py src/software_factory/orchestration/worker.py tests/test_activities.py
git commit -m "feat: grava os tokens de cada sessão do Claude no SQLite"
```

### Task 6: `upsert_comment` no Linear

**Files:**
- Modify: `src/software_factory/orchestration/linear_activities.py`
- Modify: `src/software_factory/orchestration/models.py:129-133` (comentário de `CommentRequest.comment_id`: "UUID gerado pelo chamador: idempotência")
- Modify: `src/software_factory/orchestration/worker.py` (registrar `linear.upsert_comment`)
- Test: `tests/test_linear_activities.py`

**Interfaces:**
- Produces: activity `upsert_comment(request: CommentRequest) -> str` (id do comentário).

Reaproveite `COMMENT_EXISTS_QUERY` e `COMMENT_CREATE_MUTATION`. Nova mutation:

```graphql
mutation($id: String!, $body: String!) { commentUpdate(id: $id, input: {body: $body}) { success } }
```

- [ ] **Step 1: Escrever os testes que falham** (transport fake do httpx, como os de `post_comment`)
- `test_upsert_creates_when_missing`: a query devolve `nodes: []` → `commentCreate` com o id e o corpo.
- `test_upsert_updates_when_exists`: `nodes: [{id}]` → `commentUpdate` com o corpo novo; nenhum `commentCreate`.
- `test_upsert_update_not_success_raises`: `success: false` → `ApplicationError` (retentável).
- `test_upsert_issue_not_found_non_retryable`.

- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_linear_activities -v`
- [ ] **Step 3: Implementar** extraindo de `post_comment` um helper `_comment_exists(issue_id, comment_id) -> bool` usado pelos dois.
- [ ] **Step 4: Rodar e ver passar** — mesmo comando, PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/linear_activities.py src/software_factory/orchestration/models.py src/software_factory/orchestration/worker.py tests/test_linear_activities.py
git commit -m "feat: upsert_comment cria ou edita comentário no Linear"
```

### Task 7: Activity `token_usage_report` e helper `publish_token_usage`

**Files:**
- Create: `src/software_factory/orchestration/token_activities.py` (`TokenUsageActivities`)
- Create: `src/software_factory/orchestration/token_reporting.py` (helper de workflow, sem I/O)
- Modify: `src/software_factory/orchestration/models.py` (`UsageReportRequest(issue_id: str, final: bool = False)`)
- Modify: `src/software_factory/orchestration/worker.py` (registrar `tokens.token_usage_report` com o mesmo `TokenStore` da Task 5)
- Test: `tests/test_token_activities.py`

**Interfaces:**
- Consumes: `TokenStore.issue_usage`, `render_usage_comment`, `token_usage_comment_id`, `upsert_comment`.
- Produces:
  - activity `TokenUsageActivities(store).token_usage_report(request: UsageReportRequest) -> str` (o corpo do comentário; lê via `asyncio.to_thread`).
  - `async def publish_token_usage(issue_id: str, final: bool = False) -> None`, para usar dentro de workflows: `token_usage_report` (timeout 30s, `RetryPolicy(maximum_attempts=3)`), depois `upsert_comment` com `CommentRequest(issue_id, token_usage_comment_id(issue_id), body)` (timeout 30s, `maximum_attempts=5`). `ActivityError` → `workflow.logger.warning`, sem relançar.

- [ ] **Step 1: Escrever os testes que falham**
- `test_report_renders_from_store`: store com uma sessão `plan` → o texto contém `Planejamento:`.
- `test_report_final_has_total`.
- `test_publish_calls_report_then_upsert` (`WorkflowEnvironment` time-skipping, workflow de teste mínimo que só chama `publish_token_usage`, activities mockadas): `upsert_comment` recebe o id `token_usage_comment_id(issue_id)` e o corpo devolvido pelo report.
- `test_publish_swallows_activity_failure`: `upsert_comment` mockado sempre levanta `ApplicationError(non_retryable=True)` → o workflow de teste termina sem erro.

- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_token_activities -v`
- [ ] **Step 3: Implementar** os dois módulos e registrar no worker.
- [ ] **Step 4: Rodar e ver passar** — mesmo comando, PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/token_activities.py src/software_factory/orchestration/token_reporting.py src/software_factory/orchestration/models.py src/software_factory/orchestration/worker.py tests/test_token_activities.py
git commit -m "feat: publicação do relatório de tokens no comentário da issue"
```

### Task 8: Publicar ao fim de cada etapa

**Files:**
- Modify: `src/software_factory/orchestration/workflow.py` (depois do `finally` do kill, antes de mover para In Progress)
- Modify: `src/software_factory/orchestration/implement_workflow.py` (antes de mover para In Review; no retorno `OUTCOME_STOPPED`)
- Modify: `src/software_factory/orchestration/review_workflow.py:264-289` (`final=True` depois do `_move` para Done; `_stopped` vira `async` e publica com `final=False`, e cada `return self._stopped(...)` vira `return await self._stopped(...)`)
- Test: `tests/test_workflow.py`, `tests/test_implement_workflow.py`, `tests/test_review_workflow.py`

**Interfaces:**
- Consumes: `publish_token_usage` (Task 7). Os testes de workflow precisam registrar mocks de `token_usage_report` e `upsert_comment` nas listas de activities existentes.

- [ ] **Step 1: Escrever os testes que falham**
- `test_workflow.py::test_publishes_token_usage_before_in_progress`: ordem das chamadas mockadas: `commit_plan` → `kill_sandbox` → `token_usage_report(final=False)` → `upsert_comment` → `move_issue(In Progress)`.
- `test_implement_workflow.py::test_publishes_before_in_review` e `test_publishes_when_stopped`.
- `test_review_workflow.py::test_publishes_final_after_done`: `token_usage_report` recebe `final=True` depois do `move_issue(Done)`; `test_publishes_when_stopped` com `final=False`.
- `test_review_workflow.py::test_publish_failure_does_not_fail_workflow`: `upsert_comment` sempre falha → resultado `OUTCOME_MERGED` e a issue movida para Done.

- [ ] **Step 2: Rodar e ver falhar** — `PYTHONPATH=src python3 -m unittest tests.test_workflow tests.test_implement_workflow tests.test_review_workflow -v`
- [ ] **Step 3: Implementar** as chamadas e ajustar os mocks dos testes existentes.
- [ ] **Step 4: Rodar a suíte inteira** — `PYTHONPATH=src python3 -m unittest discover -s tests`, tudo PASS.
- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/workflow.py src/software_factory/orchestration/implement_workflow.py src/software_factory/orchestration/review_workflow.py tests/test_workflow.py tests/test_implement_workflow.py tests/test_review_workflow.py
git commit -m "feat: comentário de tokens ao fim do planejamento, implementação e review"
```

### Task 9: Documentação

**Files:**
- Modify: `CLAUDE.md` (Stack: **Persistência:** SQLite (`sqlite3`) só para o uso de tokens, em `TOKEN_USAGE_DB`; tirar "persistência" de "ainda não definidos" e deixar só deploy; Estrutura: `token_usage.py`, `token_store.py`, `token_activities.py`, `token_reporting.py`; seção "Webhook do Linear": o comentário de tokens, quando publica, o id derivado da issue e as limitações do %, incluindo o que a Task 1 encontrou sobre a frequência do `rate_limit_event`)
- Modify: `README.md` (env `TOKEN_USAGE_DB` e exemplos de consulta)

Exemplos de consulta para o README:

```bash
# tokens e custo por etapa, por issue
sqlite3 token_usage.db "SELECT issue_id, stage, COUNT(*) sessoes,
  SUM(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens) total,
  SUM(cost_usd) custo FROM sessions GROUP BY issue_id, stage"
# média de tokens por 1% da janela de 5h (com assinatura)
sqlite3 token_usage.db "SELECT AVG(t / (s*100)) FROM (SELECT SUM(input_tokens+output_tokens+cache_creation_tokens+cache_read_tokens) t,
  MAX(w.util_last)-MIN(w.util_first) s FROM sessions JOIN session_windows w ON w.session_key = sessions.key
  GROUP BY sessions.issue_id, sessions.stage, w.resets_at HAVING s > 0)"
```

- [ ] **Step 1: Atualizar os dois arquivos**
- [ ] **Step 2: Conferir que a suíte segue verde** — `PYTHONPATH=src python3 -m unittest discover -s tests`, PASS.
- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: relatório de tokens e SQLite de uso"
```
