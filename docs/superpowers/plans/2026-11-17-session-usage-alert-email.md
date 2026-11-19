# Aviso por email ao usar 80% da sessão do Claude — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando uma sessão do Claude Code no sandbox mostrar que a janela de 5 horas da assinatura passou de 80% de uso, o worker manda um email avisando o percentual e quando o limite reseta. É um email por janela, mesmo com vários workflows rodando ao mesmo tempo.

**Architecture:** O `claude -p --output-format stream-json --verbose` já emite eventos `rate_limit_event` no stdout que o worker recebe pelo `on_stdout` do E2B. Um `UsageWatcher` (puro) remonta as linhas a partir dos chunks e detecta a travessia dos 80% na janela `five_hour`. Ao detectar, o `UsageAlertNotifier` inicia um `UsageAlertWorkflow` com id `usage-alert-five-hour-<resetsAt>` e `WorkflowIDReusePolicy.REJECT_DUPLICATE`. O id do Temporal é a deduplicação entre sessões, sem persistência nova. O workflow chama a activity `send_usage_alert`, que envia por SMTP (`smtplib`, stdlib) com retry do Temporal.

**Tech Stack:** Python 3.12, temporalio (`start_workflow` com `id_reuse_policy`), `smtplib`/`email.message`/`zoneinfo` (stdlib), unittest. Nenhuma dependência nova.

**Spec:** pedido do usuário em 2026-11-17: "ao utilizar 80% da sessao durante alguma implementacao, deve ser enviado um email <endereço do usuário> avisando que foi utilizado os 80% da sessao e quando o limite sera resetado". Respostas do usuário: envio por **SMTP (smtplib)** e monitorar **todas as sessões** do Claude (planejamento, implementação, análise, correção e review).

## Decisões tomadas neste plano (não estão na spec)

- **"Sessão" = janela `five_hour` da assinatura.** Ela só existe com `CLAUDE_CODE_OAUTH_TOKEN`. Com `ANTHROPIC_API_KEY` o Claude Code não emite `rate_limit_event` de assinatura, e o aviso simplesmente não dispara. As janelas de 7 dias são ignoradas.
- **Formato do evento** (conferido no Claude Code 2.1.327, schema `rate_limit_event`): `{"type":"rate_limit_event","rate_limit_info":{"status","resetsAt","rateLimitType","utilization","unifiedWindows":{"five_hour":{"utilization","resetsAt","status","surpassedThreshold"},...}}}`. `utilization` vai de 0 a 1 e pode passar de 1. `resetsAt` é epoch em segundos. Lê `unifiedWindows.five_hour` primeiro. Se não houver (versão mais velha do CLI no template `claude`), usa o topo, mas só com `rateLimitType == "five_hour"` e `utilization` presente.
- **Dedup:** dentro da sessão, o watcher emite no máximo um alerta por `resetsAt`. Entre sessões e workflows, o id `usage-alert-five-hour-<resetsAt>` com `REJECT_DUPLICATE` faz o segundo `start_workflow` levantar `WorkflowAlreadyStartedError`. Isso vale enquanto o run estiver na retenção do namespace (24h no dev server, bem mais que as 5h da janela).
- **O alerta nunca derruba a sessão do Claude.** Erro ao iniciar o workflow só vai para o log. As tasks de notificação são aguardadas no `finally` do `_run_claude`, com teto de `NOTIFY_WAIT_S = 30`. Assim elas não ficam órfãs quando a activity termina.
- **Destinatário em env (`ALERT_EMAIL_TO`), nunca no código.** O endereço é dado pessoal e o repo é versionado. Vai só no `.env`, que está no `.gitignore`.
- **SMTP opcional:** sem `SMTP_HOST` o aviso fica desligado (log `aviso de uso desligado` no startup do worker), e quem usa API key não precisa configurar nada. Com `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` e `ALERT_EMAIL_TO` passam a ser obrigatórias. Para Gmail: `smtp.gmail.com`, porta 587 com STARTTLS e senha de app.
- **Horário do reset** formatado em `ALERT_TIMEZONE` (default `America/Sao_Paulo`) como `17/11/2026 às 15:00 (America/Sao_Paulo)`.
- **`send_usage_alert` não é idempotente** (SMTP não tem chave de idempotência). Se o envio der certo e a resposta se perder, o retry pode mandar um email duplicado. Isso é aceito: duplicar é melhor que perder o aviso.
- `run_tests` **não** é monitorado. A saída do `TEST_COMMAND` não é stream-json do Claude e poderia forjar um evento.

## Global Constraints

- Nenhuma dependência nova: `smtplib`, `email.message.EmailMessage`, `zoneinfo`, `asyncio.to_thread`.
- Workflow determinístico: o `UsageAlertWorkflow` só chama a activity. SMTP e `start_workflow` ficam fora do workflow.
- Constantes: `SESSION_WINDOW = "five_hour"`, `SESSION_ALERT_THRESHOLD = 0.8`, `NOTIFY_WAIT_S = 30`, `DEFAULT_SMTP_PORT = 587`, `DEFAULT_ALERT_TIMEZONE = "America/Sao_Paulo"`.
- Id do workflow: `usage-alert-five-hour-<resets_at>` (o `_` da janela vira `-`).
- Envs novas do worker: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM` (default = `SMTP_USERNAME`), `ALERT_EMAIL_TO`, `ALERT_TIMEZONE`.
- Logs, mensagens, email e docs em português.
- Testes: `PYTHONPATH=src python3 -m unittest discover -s tests`.

## Review Focus

1. **Linha do stream-json partida entre dois chunks do `on_stdout`:** o evento tem que ser detectado mesmo assim. Teste em Task 1: `test_line_split_across_chunks`.
2. **Duas sessões em paralelo (lentes da review, ou implement + review de issues diferentes) cruzam os 80% na mesma janela:** sai um email só. Teste em Task 3: o segundo `notify` recebe `WorkflowAlreadyStartedError` e devolve `False`.
3. **Temporal fora ou SMTP mal configurado no meio de uma implementação:** a sessão do Claude segue e a activity devolve o resultado normal. Teste em Task 4: o notifier levanta `RuntimeError` e o `implement_plan` devolve o texto do `result`.
4. **Uso sobe de 80% para 85% e 90% na mesma janela:** nada de email novo. Teste em Task 1: `test_one_alert_per_reset` com três eventos.
5. **CLI mais velho no template sem `unifiedWindows`:** cai no topo do evento. Teste em Task 1: `test_top_level_fallback`. Em contrapartida, `rateLimitType == "seven_day"` no topo não dispara: `test_ignores_other_windows`.

---

### Task 1: Detecção do uso no stream-json

**Files:**
- Create: `src/software_factory/orchestration/usage.py`
- Modify: `src/software_factory/orchestration/models.py` (dataclass `UsageAlert`)
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces:
  - `models.UsageAlert` (dataclass frozen): `window: str`, `utilization: float`, `resets_at: int`, `source: str = ""`
  - `usage.SESSION_WINDOW = "five_hour"`, `usage.SESSION_ALERT_THRESHOLD = 0.8`
  - `usage.session_usage(event: dict[str, Any]) -> UsageAlert | None`: o alerta se o evento é `rate_limit_event` da janela de sessão com `utilization >= 0.8`, senão `None`
  - `usage.usage_alert_workflow_id(alert: UsageAlert) -> str`: `f"usage-alert-{alert.window.replace('_', '-')}-{alert.resets_at}"`
  - `usage.UsageWatcher(source: str)` com `feed(chunk: str) -> list[UsageAlert]`: guarda o resto sem `\n` para o próximo chunk, ignora linha que não é JSON e emite no máximo um alerta por `resets_at`; preenche `source`

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_usage.py`, com um helper `rate_event(**info) -> str` que devolve a linha JSON com `\n`:
  - `test_unified_window`: `unifiedWindows.five_hour = {"utilization": 0.83, "resetsAt": 1795440000}` → `UsageAlert("five_hour", 0.83, 1795440000)`
  - `test_below_threshold`: `utilization 0.79` → `None`; `0.8` exato → alerta
  - `test_top_level_fallback`: sem `unifiedWindows`, `rateLimitType "five_hour"`, `utilization 0.9`, `resetsAt 1795440000` → alerta
  - `test_ignores_other_windows`: topo com `rateLimitType "seven_day"`, `utilization 0.95` → `None`; `unifiedWindows` só com `seven_day` acima de 0.8 → `None`
  - `test_ignores_other_events`: `{"type": "result", ...}` e topo sem `utilization` → `None`
  - `test_line_split_across_chunks`: a linha cortada ao meio em dois `feed` → `[]` no primeiro e um alerta no segundo
  - `test_one_alert_per_reset`: eventos com 0.8, 0.85 e 0.9 e o mesmo `resetsAt` → um alerta no total; um `resetsAt` novo → outro alerta
  - `test_non_json_lines_ignored`: `"warning: foo\n"` → `[]`
  - `test_source_and_workflow_id`: `UsageWatcher("implement-issue-abc/implement_plan")` → `alert.source` igual; `usage_alert_workflow_id(alert) == "usage-alert-five-hour-1795440000"`

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_usage -v`
Expected: FAIL com `ModuleNotFoundError: software_factory.orchestration.usage`

- [ ] **Step 3: Implementar `UsageAlert` em `models.py` e `usage.py`** com as assinaturas acima. `session_usage` lê `rate_limit_info.unifiedWindows.five_hour` (`utilization`, `resetsAt`) e, se não houver, o topo com `rateLimitType == SESSION_WINDOW`. Valores não numéricos dão `None`.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_usage -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/usage.py src/software_factory/orchestration/models.py tests/test_usage.py
git commit -m "feat: detecção do uso da sessão do Claude no stream-json"
```

---

### Task 2: Configuração e envio do email por SMTP

**Files:**
- Create: `src/software_factory/notify/__init__.py`, `src/software_factory/notify/email.py`
- Modify: `src/software_factory/config.py` (`FactorySettings.alert_email`)
- Modify: `.env.example`
- Test: `tests/test_email.py`, `tests/test_activities.py` (classe `TestFactorySettings`)

**Interfaces:**
- Consumes: `models.UsageAlert` (Task 1)
- Produces:
  - `notify.email.EmailSettings` (dataclass frozen): `host: str`, `port: int`, `username: str`, `password: str`, `sender: str`, `to: str`, `timezone: str`; `@classmethod from_env() -> EmailSettings | None` (`None` sem `SMTP_HOST`)
  - `notify.email.build_usage_alert_email(alert: UsageAlert, timezone: str) -> tuple[str, str]` → `(subject, body)`
  - `notify.email.send_email(settings: EmailSettings, subject: str, body: str, smtp_cls: Any = smtplib.SMTP) -> None` (síncrona; quem chama usa `asyncio.to_thread`)
  - `FactorySettings.alert_email: EmailSettings | None = None`, preenchido em `from_env` com `EmailSettings.from_env()`

Texto fixo do email:

```
Assunto: [software-factory] 80% da sessão do Claude usada
Corpo:
A sessão atual do Claude Code (janela de 5 horas da assinatura) chegou a {pct}% de uso.
O limite será resetado em {dd/mm/aaaa} às {HH:MM} ({timezone}).

Detectado em: {source}
```

`pct = round(alert.utilization * 100)`. A linha `Detectado em` sai quando `source` é vazio.

- [ ] **Step 1: Escrever os testes que falham**
  - `test_email.py::test_build_usage_alert_email`: `UsageAlert("five_hour", 0.834, 1795440000, "implement-issue-abc/implement_plan")` com `"America/Sao_Paulo"` → o assunto é igual ao fixo e o corpo contém `"83% de uso"`, a data e hora de `1795440000` em São Paulo no formato `dd/mm/aaaa às HH:MM` (calcule o esperado no teste com `datetime.fromtimestamp(..., ZoneInfo(...))`) e `"Detectado em: implement-issue-abc/implement_plan"`
  - `test_email.py::test_send_email`: `FakeSMTP` (context manager que grava `host`, `port`, `starttls()`, `login(user, pw)`, `send_message(msg)`) → `starttls` antes de `login`, `msg["From"] == sender`, `msg["To"] == to`, `msg["Subject"]` e o corpo iguais
  - `test_email.py::test_from_env`: sem `SMTP_HOST` → `None`; com `SMTP_HOST` e sem `ALERT_EMAIL_TO` → `RuntimeError`; completo, sem `SMTP_PORT`/`ALERT_EMAIL_FROM`/`ALERT_TIMEZONE` → `port == 587`, `sender == SMTP_USERNAME`, `timezone == "America/Sao_Paulo"` (use `unittest.mock.patch.dict(os.environ, ..., clear=True)`)
  - `test_activities.py::TestFactorySettings::test_alert_email_optional`: `FactorySettings.from_env()` sem `SMTP_HOST` → `alert_email is None`

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_email tests.test_activities.TestFactorySettings -v`
Expected: FAIL com `ModuleNotFoundError: software_factory.notify`

- [ ] **Step 3: Implementar `notify/email.py` e o campo em `config.py`**, reaproveitando `config._required` para as envs obrigatórias. `send_email` usa `smtp_cls(host, port, timeout=30)` como context manager, `starttls()`, `login`, `send_message(EmailMessage)`. Constantes `DEFAULT_SMTP_PORT = 587` e `DEFAULT_ALERT_TIMEZONE = "America/Sao_Paulo"`. No `.env.example`, adicione um bloco comentado com as sete envs, o exemplo do Gmail (senha de app, exige 2FA) e a nota "sem SMTP_HOST o aviso de uso fica desligado; só funciona com CLAUDE_CODE_OAUTH_TOKEN".

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_email tests.test_activities.TestFactorySettings -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/notify src/software_factory/config.py .env.example tests/test_email.py tests/test_activities.py
git commit -m "feat: envio de email por SMTP para o aviso de uso"
```

---

### Task 3: Workflow do aviso, activity de envio e notifier com dedup

**Files:**
- Create: `src/software_factory/orchestration/usage_alert_workflow.py` (`UsageAlertWorkflow`)
- Create: `src/software_factory/orchestration/notify_activities.py` (`NotifyActivities`, `UsageAlertNotifier`)
- Test: `tests/test_usage_alert.py`

**Interfaces:**
- Consumes: `UsageAlert`, `usage_alert_workflow_id` (Task 1); `EmailSettings`, `build_usage_alert_email`, `send_email` (Task 2)
- Produces:
  - `UsageAlertWorkflow.run(alert: UsageAlert) -> None`: `execute_activity("send_usage_alert", alert, start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=10, initial_interval=timedelta(seconds=10), backoff_coefficient=2))`
  - `NotifyActivities(email: EmailSettings, send: Callable = send_email)` com `@activity.defn async def send_usage_alert(self, alert: UsageAlert) -> None` (monta com `build_usage_alert_email(alert, email.timezone)` e envia via `asyncio.to_thread`)
  - `UsageAlertNotifier(temporal: Any, task_queue: str)` com `async def notify(self, alert: UsageAlert) -> bool`: `start_workflow(UsageAlertWorkflow.run, alert, id=usage_alert_workflow_id(alert), task_queue=..., id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)`. Devolve `True` quando inicia e `False` em `WorkflowAlreadyStartedError` (log info). Qualquer outra exceção também dá `False`, com `logger.warning(..., exc_info=True)`, e nunca propaga.

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_usage_alert.py`
  - `test_workflow_sends_alert`: `WorkflowEnvironment.start_time_skipping()` + `Worker` com `UsageAlertWorkflow` e uma activity mock `send_usage_alert` (`@activity.defn(name="send_usage_alert")`) que grava o alerta recebido. Ela é chamada uma vez com o mesmo `UsageAlert`.
  - `test_activity_formats_and_sends`: `NotifyActivities(EMAIL, send=fake_send)` chamada direto (via `ActivityEnvironment().run`). `fake_send` recebe `EMAIL` e o assunto fixo da Task 2.
  - `test_notifier_starts_with_reject_duplicate`: `FakeClient.start_workflow` grava args e kwargs → `True`, `id == "usage-alert-five-hour-1795440000"`, `id_reuse_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE`, `task_queue` repassado
  - `test_notifier_duplicate`: `start_workflow` levanta `WorkflowAlreadyStartedError("id", "UsageAlertWorkflow")` → `False`
  - `test_notifier_swallows_errors`: `start_workflow` levanta `RuntimeError("temporal fora")` → `False`, sem exceção (`assertLogs` em WARNING)

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_usage_alert -v`
Expected: FAIL com `ModuleNotFoundError`

- [ ] **Step 3: Implementar os dois módulos.** No workflow, importe os models com `workflow.unsafe.imports_passed_through()`, como em `reconcile_workflow.py`.

- [ ] **Step 4: Rodar e ver passar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_usage_alert -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/software_factory/orchestration/usage_alert_workflow.py src/software_factory/orchestration/notify_activities.py tests/test_usage_alert.py
git commit -m "feat: UsageAlertWorkflow com dedup por janela de uso"
```

---

### Task 4: Monitorar toda sessão do Claude e registrar no worker

**Files:**
- Modify: `src/software_factory/orchestration/activities.py` (`SandboxActivities.__init__`, novo `_run_claude`; trocar `_run_with_heartbeat` por `self._run_claude` em `generate_plan`, `implement_plan`, `analyze_failure` e `implement_fix`. `run_tests` continua no `_run_with_heartbeat`)
- Modify: `src/software_factory/orchestration/review_activities.py` (as três chamadas de `_run_with_heartbeat` viram `self.sandbox._run_claude`)
- Modify: `src/software_factory/orchestration/worker.py`
- Modify: `README.md`, `CLAUDE.md`
- Test: `tests/test_activities.py`, `tests/test_review_activities.py`

**Interfaces:**
- Consumes: `UsageWatcher` (Task 1); `UsageAlertNotifier`, `NotifyActivities`, `UsageAlertWorkflow` (Task 3); `FactorySettings.alert_email` (Task 2)
- Produces:
  - `SandboxActivities(settings, sandbox_cls=AsyncSandbox, usage_notifier: UsageAlertNotifier | None = None)`
  - `SandboxActivities._run_claude(self, sandbox: Any, cmd: str, timeout: int) -> Any`: igual ao `_run_with_heartbeat`, mas o `on_stdout` também alimenta um `UsageWatcher(f"{info.workflow_id}/{info.activity_type}")`. Cada alerta vira `asyncio.create_task(notifier.notify(alert))`. No `finally`, roda `asyncio.wait(tasks, timeout=NOTIFY_WAIT_S)`. Sem notifier, só faz o heartbeat.
  - `_run_with_heartbeat(sandbox, cmd, timeout, on_line: Callable[[str], None] | None = None)`: o `on_stdout` faz heartbeat e repassa o chunk para `on_line`, se houver

- [ ] **Step 1: Escrever os testes que falham** em `tests/test_activities.py`, com um `FakeNotifier` (`async notify(alert)` grava e devolve `True`, ou levanta se configurado). O stdout do `FakeCommands` é uma linha `rate_limit_event` com 0.85, seguida do evento `result` de sucesso. Use o `ActivityEnvironment` que os testes atuais já usam.
  - `test_implement_plan_notifies_usage`: `implement_plan` → `notifier.alerts == [UsageAlert("five_hour", 0.85, <reset>, source=<workflow_id>/implement_plan)]` e o retorno é o texto do `result`
  - `test_usage_notifier_failure_does_not_fail_session`: o notifier levanta `RuntimeError` → `implement_plan` devolve o texto normalmente
  - `test_run_tests_not_watched`: a saída do `TEST_COMMAND` contém a linha `rate_limit_event` → `run_tests` não chama o notifier
  - `test_no_notifier`: `SandboxActivities(SETTINGS, ...)` sem notifier com o mesmo stdout → funciona como hoje
  - Em `tests/test_review_activities.py`, `test_review_pull_notifies_usage`: a resposta das lentes traz a linha de 0.85. Cada lente tem o seu watcher, então o `FakeNotifier` recebe um alerta por lente, todos com o mesmo `resets_at`. Quem deduplica entre elas é o Temporal (Task 3). Asserção: `len(alerts) >= 1` e `{a.resets_at for a in alerts} == {<reset>}`.

- [ ] **Step 2: Rodar e ver falhar**

Run: `PYTHONPATH=src python3 -m unittest tests.test_activities tests.test_review_activities -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'usage_notifier'`)

- [ ] **Step 3: Implementar `_run_claude` e trocar as chamadas.** Em `worker.py`, quando `settings.alert_email` existe: `notifier = UsageAlertNotifier(client, settings.task_queue)`, `SandboxActivities(settings, usage_notifier=notifier)`, `UsageAlertWorkflow` na lista de workflows e `NotifyActivities(settings.alert_email).send_usage_alert` nas activities. Sem `alert_email`: `logging.info("aviso de uso desligado: SMTP_HOST não definida")` e nada é registrado.

- [ ] **Step 4: Rodar a suíte inteira**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: `OK`, sem falhas

- [ ] **Step 5: Atualizar a documentação**
  - `CLAUDE.md`, "Stack definida": linha **Aviso de uso** dizendo que é SMTP via `smtplib` (stdlib), opcional (`SMTP_HOST`), só com `CLAUDE_CODE_OAUTH_TOKEN`, um email por janela `five_hour` deduplicado pelo id `usage-alert-five-hour-<resetsAt>` com `REJECT_DUPLICATE`
  - `CLAUDE.md`, "Estrutura": `notify/email.py`, `orchestration/usage.py`, `usage_alert_workflow.py` e `notify_activities.py`
  - `README.md`: as envs novas na tabela de variáveis do worker e um parágrafo sobre o aviso (Gmail com senha de app, limitação do email duplicado em retry, `run_tests` fora do monitoramento)

- [ ] **Step 6: Commit**

```bash
git add src/software_factory/orchestration/activities.py src/software_factory/orchestration/review_activities.py src/software_factory/orchestration/worker.py tests/test_activities.py tests/test_review_activities.py README.md CLAUDE.md
git commit -m "feat: aviso por email ao usar 80% da sessão do Claude"
```
