# CLAUDE.md

## Visão geral

Software factory experimental (fase de teste). O objetivo é automatizar o ciclo
**issue → plano de implementação → implementação → Pull Request**:

1. Criar uma issue no Linear ou pegar uma issue já existente.
2. Gerar um plano de implementação a partir da issue.
3. Executar o plano (implementar o código).
4. Abrir a PR com as mudanças.

## Stack definida

- **Linguagem:** Python 3.12+ (pacote `software_factory` em `src/`, build com setuptools, ambiente com `uv`).
- **Orquestrador:** [Temporal](https://docs.temporal.io/develop/python) (`temporalio` SDK). Cada etapa do fluxo deve ser modelada como workflow/activities.
- **Harness de execução:** Claude Code — é quem efetivamente planeja e implementa as mudanças, invocado a partir das activities do Temporal.
- **Fonte de tarefas:** Linear (issues), recebidas via webhook.
- **HTTP:** FastAPI + uvicorn (servidor de webhooks).
- **Sandbox:** [E2B](https://docs.e2b.dev) (SDK `e2b`, `AsyncSandbox`), template `claude`
  (Claude Code pré-instalado). **Um sandbox por sessão** (planejamento, análise de falha ou correção; cada
  um com orçamento próprio de 55 min), sempre destruído no `finally`. Plano Hobby: ~1h contínua por sandbox — timeouts das activities ficam abaixo disso.
- **Invocação do Claude Code:** CLI headless dentro do sandbox
  (`claude -p --output-format stream-json --verbose`). Auth: exatamente uma de
  `ANTHROPIC_API_KEY` (API) ou `CLAUDE_CODE_OAUTH_TOKEN` (assinatura, `claude setup-token`;
  zona cinzenta nos termos para automação em sandbox de terceiro).
- **Git/PR:** GitHub; um único repo alvo (`TARGET_REPO=owner/repo`), clonado no sandbox com
  `GITHUB_TOKEN` fine-grained restrito a esse repo, com **Contents: Read and write** (push da
  branch) e **Pull requests: Read and write** (abertura da PR). API REST do GitHub
  (`https://api.github.com`) chamada com `httpx` puro (`github/client.py`, sem SDK).
- **Testes do repo alvo:** `TEST_COMMAND` (env obrigatória do worker), rodado pelo orquestrador
  no sandbox depois da implementação; exit 0 = verde. A PR só abre com verde.
- **Linear API:** só GraphQL (`https://api.linear.app/graphql`, não há REST), chamada com `httpx`
  puro (`linear/client.py`, sem SDK); personal API key em `LINEAR_API_KEY` (worker), com escrita em issues.
- **Review:** skills `requesting-code-review` (lente `correctness`) e `receiving-code-review` (triagem) do
  superpowers 7.0.2, vendorizadas ao lado (MIT); a lente `security` usa prompt próprio.
- **Planejamento:** skill `writing-plans` do superpowers 7.0.0, vendorizada em
  `orchestration/skills/` (MIT) e copiada para `~/.claude/skills/` no sandbox — sem instalar plugin via rede.
- **Correção de testes vermelhos:** skill `systematic-debugging` do superpowers 7.0.2, vendorizada
  ao lado (MIT); `install_skills` copia tudo que está em `orchestration/skills/`.
- **Aviso de uso da sessão:** email por SMTP com `smtplib` (stdlib, STARTTLS; ex.: Gmail com senha
  de app), opcional (sem `SMTP_HOST` fica desligado). Só com `CLAUDE_CODE_OAUTH_TOKEN`: toda sessão do
  Claude (menos `run_tests`) lê os `rate_limit_event` do stream-json e, com a janela `five_hour` em
  ≥ 80%, inicia o `UsageAlertWorkflow` com id `usage-alert-five-hour-<resetsAt>` e
  `ALLOW_DUPLICATE_FAILED_ONLY` (um email por janela, sem persistência; um run que falhou deixa a
  próxima travessia tentar de novo). Erro no aviso nunca derruba a sessão.
- **Persistência:** SQLite (`sqlite3`, stdlib), só para o uso de tokens das sessões do Claude; arquivo em
  `TOKEN_USAGE_DB` (default `token_usage.db`, no `.gitignore`). Assume um único worker gravando.

Ainda **não definidos** (não assumir sem confirmar com o usuário): deploy. Ao introduzir qualquer tecnologia nova,
pergunte antes e atualize esta seção.

## Estrutura

```
src/software_factory/
  __init__.py         # __version__
  __main__.py         # entrypoint (script `software-factory`): carrega .env e sobe o uvicorn
  api.py              # create_app(): FastAPI com /health e routers
  config.py           # Settings (webhook) e FactorySettings (worker) lidas de env
  linear/
    client.py         # LinearClient: POST GraphQL com httpx; erros → ApplicationError (retentável ou não)
    signature.py      # validação HMAC do Linear-Signature + anti-replay
    events.py         # LinearIssue, should_process (filtro por label), should_implement/should_review, parse_issue,
                      # CommentReply e parse_reply (reply de comentário → HumanAnswer)
    webhook.py        # POST /webhooks/linear; handle_issue inicia o PlanIssueWorkflow, o ImplementIssueWorkflow ou o
                      # ReviewIssueWorkflow; handle_reply envia o signal human_answer aos workflows de implementação e review
  notify/
    email.py          # EmailSettings (SMTP_*, ALERT_EMAIL_*, ALERT_TIMEZONE), build_usage_alert_email, send_email
  github/
    client.py         # GitHubClient: REST com httpx (default_branch, get_file, find_open_pull, create_pull)
  orchestration/
    models.py         # dataclasses de entrada/saída (PlanIssueInput, PlanRequest, CommitPlanRequest,
                      # MoveIssueRequest, PlanResult, CloneBranchRequest, ImplementRequest, TestRunResult,
                      # CommitImplementationRequest, PullRequestRequest, ImplementResult e, do loop de correção,
                      # FixAttempt, Clarification, AnalyzeRequest, FixAnalysis, FixRequest, CommentRequest,
                      # HumanAnswer, UsageAlert, UsageReportRequest), STATE_* e OUTCOME_*
    activities.py     # SandboxActivities: create_sandbox, clone_repo, generate_plan, commit_plan,
                      # clone_branch, implement_plan, analyze_failure, implement_fix, run_tests,
                      # commit_implementation, kill_sandbox; install_skills (copia orchestration/skills/);
                      # _run_claude (sessão do Claude + aviso de uso + registro de tokens)
    usage.py          # session_usage, UsageWatcher (rate_limit_event → UsageAlert), usage_alert_workflow_id
    notify_activities.py  # NotifyActivities.send_usage_alert; UsageAlertNotifier (inicia o workflow, dedup)
    usage_alert_workflow.py  # UsageAlertWorkflow: envia o email de 80% da sessão (retry do Temporal)
    token_usage.py    # TokenMeter (stream-json → tokens, custo, modelo, amostras da janela), session_share,
                      # StageUsage/IssueUsage, render_usage_comment, stage_for_workflow, token_usage_comment_id
    token_store.py    # TokenStore (SQLite), SessionRecord; tabelas `sessions` e `session_windows`
    token_activities.py   # TokenUsageActivities.token_usage_report (lê o SQLite e devolve o texto do comentário, `render_usage_comment`)
    token_reporting.py    # publish_token_usage: helper dos workflows (report → upsert_comment; falha só loga)
    linear_activities.py  # LinearActivities: move_issue (muda o status por nome, no time da issue),
                          # post_comment, upsert_comment (cria ou edita pelo id) e create_issue (idempotentes,
                          # com UUID gerado pelo cliente)
    review_activities.py  # ReviewActivities(sandbox): review_pull (duas lentes + triagem), implement_review_fix
    review_prompts.py     # prompts da review, parse_review, REVIEW_DIR, FINDING_DETAIL_CAP,
                          # SENSITIVE_PATTERNS e sensitive_files
    sessions.py       # SessionWorkflow: base compartilhada (sessões, perguntas, espera de resposta) do implement e do review
    github_activities.py  # GitHubActivities: plan_exists, pull_request_info, open_pull_request, merge_pull_request
    workflow.py       # PlanIssueWorkflow: In Planning → sandbox → clone → plano → commit → kill (finally) → In Progress
    implement_workflow.py # ImplementIssueWorkflow: plano existe? → sandbox → clone da branch → implementa →
                      # testes → (verde: push → PR → In Review; vermelho: loop analisar → planejar → corrigir)
    review_workflow.py # ReviewIssueWorkflow: In Review → review → corrige em loop → follow-ups → merge squash → Done
    skills/writing-plans/, skills/systematic-debugging/, skills/requesting-code-review/, skills/receiving-code-review/  # vendorizadas do superpowers (SKILL.md + LICENSE)
    dispatch.py       # KIND_PLAN/IMPLEMENT/REVIEW, ISSUE_WORKFLOWS, issue_workflow_id, start_issue_workflow
                      # (usado pelo webhook e pela reconciliação)
    reconcile.py      # regras puras: pending_issue (status → etapa) e trigger_time
    reconcile_activities.py # ReconcileActivities(linear, temporal, task_queue): list_pending_issues,
                            # start_missed_workflow
    reconcile_workflow.py   # ReconcileIssuesWorkflow (id reconcile-issues), iniciado no startup do servidor
    worker.py         # entrypoint `software-factory-worker`
tests/                # unittest; payloads.py tem fixtures no formato do Linear
```

## Webhook do Linear

- Gatilho do planejamento: issue `create` com label `Feature` ou `Bug` (case-insensitive), ou
  `update` em que uma dessas labels foi adicionada (`updatedFrom.labelIds`).
- Gatilho da implementação: `update` com `stateId` em `updatedFrom` e `data.state.name ==
  "In Progress"` (case-insensitive), em issue com label de gatilho. Evento que adiciona a label
  **e** muda o status dispara só o planejamento.
- Gatilho da review: `update` com `stateId` em `updatedFrom` e `data.state.name == "In Review"`
  (case-insensitive), em issue com label de gatilho. Ordem de despacho: planejamento > implementação > review.
  Inicia o `ReviewIssueWorkflow` com id `review-issue-<issue.id>`.
- Gatilho da resposta: `Comment` `create` com `parentId` (reply na thread) numa issue; `handle_reply`
  envia o signal `human_answer` aos workflows `implement-issue-<issue.id>` **e** `review-issue-<issue.id>`,
  ignorando `NOT_FOUND` em cada um (o que não tem pergunta pendente ignora o signal); algum aceitou →
  `{"status": "accepted"}`; nenhum rodando ou comentário fora de thread → `ignored`; outro erro → 503.
  O webhook do Linear precisa dos recursos **Issues** e **Comments**.
- Eventos ignorados retornam 200 para o Linear não reenviar.
- `handle_issue` inicia o `PlanIssueWorkflow` com id `plan-issue-<issue.id>` (ou o
  `ImplementIssueWorkflow` com id `implement-issue-<issue.id>`, ou o `ReviewIssueWorkflow` com id
  `review-issue-<issue.id>`); se já houver um rodando responde
  `{"status": "duplicate"}`; se o Temporal estiver fora, 503 (o Linear reenvia).
- O workflow hoje **gera o plano** com a skill `writing-plans` e o **commita** em
  `docs/superpowers/plans/<IDENTIFIER>.md` na branch `factory/<IDENTIFIER>` (push feito pela
  activity `commit_plan`; o Claude só tem permissão `Edit` nesse arquivo, sem `Bash`). Se a branch
  já existir no remoto com outro conteúdo, falha sem force push.
- O `ImplementIssueWorkflow` confere que a issue segue em `In Progress` (senão encerra), checa o
  plano via API do GitHub antes de criar sandbox (sem plano → move para `In Planning`), clona
  `factory/<IDENTIFIER>` (`--depth 50`, para a análise ver o histórico recente) com o token **fora** do `origin` (defesa parcial: o push roda depois no
  mesmo sandbox em que o Claude teve `Bash`, então o token deve ser tratado como exposto), roda o Claude com `Bash`
  (`implement_plan`, 30 min, sem retry), roda `TEST_COMMAND` (`run_tests`; timeout de 10 min =
  vermelho) e destrói o sandbox. Commit/push do orquestrador (`commit_plan`, `commit_implementation`)
  rodam com `-c core.hooksPath=/dev/null`. Falha da sessão inicial (`implement_plan`) falha o workflow.
  Verde: push (`commit_implementation`, sem force push; falha se o Claude não mudou nada), abre a
  PR `factory/<ID>` → branch default (reaproveita PR aberta) e move para `In Review`.
- **Loop de correção (vermelho):** em vez de falhar, repete analisar → planejar → corrigir → testar
  até ficar verde. Cada sessão tem seu sandbox: análise (`analyze_failure`, skill
  `systematic-debugging`, escreve `analysis.md`, `plan.md` e, se precisar, `question.md` em
  `/home/user/fix`, fora do repo) e correção (`implement_fix` + `run_tests` + commit). Sessão
  vermelha faz push de checkpoint `wip: tentativa <n> de <ID> (testes vermelhos)` em
  `factory/<ID>` (sem force push); a PR só abre com verde. Só as `ATTEMPTS_IN_PROMPT = 3` últimas
  tentativas vão para o prompt (análise truncada em 3000 caracteres). Prompts proíbem
  apagar/pular/enfraquecer testes e mudar `TEST_COMMAND`.
- **Falha de sessão alimenta o loop:** `implement_fix` que falha (erro, timeout) vira tentativa
  vermelha com saída `implement_fix falhou: <msg>`, com checkpoint `wip: ... (correção falhou)` de
  melhor esforço no mesmo sandbox; `analyze_failure` que falha registra a tentativa
  `análise falhou: <msg>` e reanalisa. Ambas contam para `MAX_FIX_ATTEMPTS`. Falhas de
  `create_sandbox`, `clone_branch`, commit e `post_comment` ainda falham o workflow.
- **Perguntas humanas:** se a análise gera `question.md`, ou após `MAX_FIX_ATTEMPTS = 5` correções
  seguidas sem verde (o próprio workflow pergunta; a resposta zera o contador), o workflow posta
  um comentário no Linear (`post_comment`, id gerado com `workflow.uuid4()`; vale o id devolvido) e
  espera o signal `human_answer`. A pergunta de travamento entra nas clarificações só como rótulo curto. A resposta é um **reply na thread** da pergunta (`parentId` == id do comentário).
  Sem resposta em `HUMAN_ANSWER_TIMEOUT = 7 dias` o workflow falha.
- **Parar o loop:** mover a issue para fora de `In Progress`. O status é checado antes de cada
  tentativa e, na espera, a cada `ANSWER_POLL_INTERVAL = 1h` (retry ilimitado,
  `STATUS_CHECK_RETRY`); resultado `OUTCOME_STOPPED`, sem PR. Falha inesperada: sem compensação,
  issue fica em `In Progress`.
- **Review (`ReviewIssueWorkflow`, `In Review`):** exige PR aberta de `factory/<ID>` e o status `Done`
  no time (validado no início). Cada rodada, em um sandbox, roda primeiro `TEST_COMMAND` (`run_tests`,
  12 min, sem retry) no head clonado: vermelho mata o sandbox, entra no `_fix_until_green`, conta
  rodada e volta à review (nada vermelho é revisado nem mergeado). Verde: roda em paralelo as lentes `correctness`
  (`requesting-code-review`) e `security` (dois `claude -p` só leitura) sobre o diff inteiro da PR
  (merge-base → head, com o histórico das rodadas anteriores no prompt) e depois a triagem com
  `receiving-code-review`, que decide por achado: `fix` (bloqueia o merge), `issue` (follow-up no
  Linear), `note` (só no resumo), `reject` (falso positivo, com razão) ou `ask` (decisão grande,
  vira pergunta). Achado `minor` nunca vira `fix` (o loop converge). Follow-ups são criados **sem**
  label de gatilho (não encadeiam planejamento → implementação → review) e deduplicados entre
  rodadas. Cada rodada posta um comentário de resumo no Linear. Com `fix`, uma sessão de correção
  (+ `run_tests`, commit e push) e nova rodada. Sem `fix`: merge squash da PR **sem humano** com
  `sha` = commit revisado; se o head mudou desde a review (`head_changed`), revisa de novo sem
  perguntar, e isso conta rodada. **Arquivos sensíveis:** `review_pull` calcula, antes de qualquer
  Claude, `git diff --name-only <base_sha> HEAD` filtrado por `SENSITIVE_PATTERNS`
  (`review_prompts.py`: `CLAUDE.md` (em qualquer diretório), qualquer coisa sob `.claude/`, `.github/workflows/`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `conftest.py`, `.pre-commit-config.yaml` e `Makefile`) em `ReviewReport.sensitive_files`; se não estiver vazio, a review
  aprovada pergunta no Linear antes do merge e a resposta confirma o merge daquele sha (não pergunta
  de novo pelo mesmo sha). Título, detalhe, motivo e resumo vindos do LLM vão cercados (`_fence`) nos
  prompts. Follow-ups: chave `lens + file + title` (sem a linha), título limitado a 200 caracteres. PR que não pode ser mergeada
  (conflito, branch protection) vira pergunta no Linear com o motivo, e a resposta reinicia a review.
  Depois de `MAX_REVIEW_ROUNDS = 3` rodadas seguidas sem merge (correção, sessão de review que
  falha, testes vermelhos antes da review e `head_changed` contam)
  o workflow pergunta como seguir; a resposta zera o contador. Parar: mover a issue para fora de
  `In Review` (checado antes de cada rodada e na espera; `OUTCOME_STOPPED`, sem merge). Merge feito:
  issue vai para `Done`. A branch `factory/<ID>` não é apagada.
- Status no Linear: o workflow começa movendo a issue `To Do → In Planning` (só se estiver em
  `To Do`; senão planeja sem mover) e, depois do commit do plano e do kill do sandbox, move para
  `In Progress`. Status procurados por nome (case-insensitive) no time da issue; status inexistente
  falha sem retry. Em falha não há compensação: a issue fica em `In Planning`.
- **Relatório de tokens:** toda sessão do Claude (menos `run_tests`) grava um `SessionRecord` no SQLite
  (`SandboxActivities` recebe o `token_store`): input, output, cache write, cache read, custo estimado,
  modelo e as amostras da janela `five_hour`. A etapa é o prefixo do id do workflow: `plan-issue-` →
  `Planejamento` (`generate_plan`); `implement-issue-` → `Implementação` (`implement_plan`,
  `analyze_failure`, `implement_fix`); `review-issue-` → `Review` (lentes, triagem, `implement_review_fix` e
  o loop analisar/corrigir quando roda dentro da review). `run_tests` não é contado. Um comentário por
  issue no Linear (`upsert_comment`) é criado ou editado com os totais por etapa, publicado
  (`publish_token_usage`) depois do commit do plano (antes de `In Progress`), depois de abrir a PR
  (antes de `In Review`), ao parar a implementação, no merge (depois de `Done`, com `final=True`, que
  acrescenta a linha `Total:`) e ao parar a review. A linha `Total:` só aparece no merge → `Done` feito pela fábrica; issue movida para
  `Done` à mão sai da review pelo caminho de parada, sem o `Total:`. Workflow que falha não publica, mas os registros
  ficam no banco e entram na próxima publicação; falha na publicação só loga warning. Id do comentário:
  UUID v4-format derivado de `sha256("token-usage:" + issue_id)`, então os três workflows acham o mesmo
  comentário. **% da janela:** a parcela da sessão é, por `resetsAt` da `five_hour`, `max(last) -
  min(first)`; só existe com `CLAUDE_CODE_OAUTH_TOKEN`, inclui outro uso da mesma conta no intervalo e
  tem granularidade de 1%. Spike (2026-11-18): o Claude emite um `rate_limit_event` após cada resposta
  da API, com granularidade de 0,01; sem evento `result`, o fallback soma o `usage` dos eventos
  `assistant` deduplicados por `message.id` (subconta output) e o registro vira parcial (`(parcial)`).
- **Reconciliação no startup:** o `lifespan` do servidor de webhooks (`start_reconcile` em `api.py`)
  inicia o `ReconcileIssuesWorkflow` (id `reconcile-issues`; já rodando ou erro → só log, o servidor
  sobe). No worker, `list_pending_issues` busca (GraphQL, paginado) issues com label de gatilho em
  `To Do` (→ plan), `In Progress` (→ implement) e `In Review` (→ review), e calcula o gatilho:
  `To Do` = o mais recente entre `createdAt` e a adição de label de gatilho no `history`; os outros
  = a última entrada no status (sem entrada, `createdAt`). `start_missed_workflow` conta
  (`count_workflows`) runs de `<prefixo>-<id>` com `StartTime >= gatilho - CLOCK_SKEW_TOLERANCE
  (2 min)`: algum → pula (falha depois do gatilho conta como consumido); nenhum → inicia pelo
  `dispatch.start_issue_workflow` (rodando → pula). `In Planning` e label adicionada fora de
  `To Do` não são recuperados. Gatilho mais antigo que a retenção do namespace
  (`describe_namespace`; 24h no dev server) é pulado com warning, porque o run pode ter saído da
  visibility. Dev server em memória reiniciado → runs falhos voltam a rodar. A varredura cobre todos
  os times que a `LINEAR_API_KEY` enxerga.

## Comandos

```bash
# setup
uv venv .venv && source .venv/bin/activate && uv pip install -e ".[dev]"

# servidor Temporal local (terminal separado) — gRPC localhost:7233, UI http://localhost:8233
temporal server start-dev --db-filename temporal.db

# worker do Temporal (lê .env; precisa de E2B_API_KEY, ANTHROPIC_API_KEY ou CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, TARGET_REPO, LINEAR_API_KEY, TEST_COMMAND;
# opcional: SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, ALERT_EMAIL_TO para o aviso de uso)
software-factory-worker

# servidor de webhooks (lê .env)
software-factory
# ou, com reload
uvicorn software_factory.api:create_app --factory --env-file .env --reload

# túnel para o Linear alcançar a máquina local
ngrok http 8000

# testes
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Convenções para o Temporal

- **Workflows devem ser determinísticos:** nada de I/O, rede, `datetime.now()`,
  `random`, threads ou chamadas ao Claude Code/Linear/Git dentro do workflow.
  Use `workflow.now()`, `workflow.random()` e mova todo efeito colateral para activities.
- Toda interação externa (Linear, Claude Code, Git, criação de PR) fica em **activities**,
  com timeouts e retry policy explícitos. Execuções do Claude Code são longas:
  configure `start_to_close_timeout` adequado e use heartbeat.
- Activities devem ser **idempotentes** (podem ser reexecutadas em retry), ex.: checar se a
  branch/PR já existe antes de criar.
- Entradas/saídas de workflows e activities como `dataclass`es serializáveis.
- Use o sandbox padrão do SDK Python; importe módulos não determinísticos com
  `workflow.unsafe.imports_passed_through()` quando necessário.
- Teste workflows com `temporalio.testing.WorkflowEnvironment` (time-skipping) e activities mockadas.

## Diretrizes gerais

- Segredos (tokens do Linear, GitHub, Anthropic) vêm de variáveis de ambiente / `.env`
  (já no `.gitignore`); nunca commitar.
- Manter o README e este arquivo atualizados quando comandos ou stack mudarem.
- Branch principal: `main`.
