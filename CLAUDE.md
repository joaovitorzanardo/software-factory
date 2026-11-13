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
  (Claude Code pré-instalado). **Um sandbox por execução de workflow (issue)**, sempre destruído
  no `finally`. Plano Hobby: ~1h contínua por sandbox — timeouts das activities ficam abaixo disso.
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
- **Planejamento:** skill `writing-plans` do superpowers 7.0.0, vendorizada em
  `orchestration/skills/` (MIT) e copiada para `~/.claude/skills/` no sandbox — sem instalar plugin via rede.

Ainda **não definidos** (não assumir sem confirmar com o usuário): persistência e deploy. Ao introduzir qualquer tecnologia nova,
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
    events.py         # LinearIssue, should_process (filtro por label), parse_issue
    webhook.py        # POST /webhooks/linear; handle_issue inicia o PlanIssueWorkflow ou o ImplementIssueWorkflow
  github/
    client.py         # GitHubClient: REST com httpx (default_branch, get_file, find_open_pull, create_pull)
  orchestration/
    models.py         # dataclasses de entrada/saída (PlanIssueInput, PlanRequest, CommitPlanRequest,
                      # MoveIssueRequest, PlanResult, CloneBranchRequest, ImplementRequest, TestRunResult,
                      # CommitImplementationRequest, PullRequestRequest, ImplementResult), STATE_* e OUTCOME_*
    activities.py     # SandboxActivities: create_sandbox, clone_repo, generate_plan, commit_plan,
                      # clone_branch, implement_plan, run_tests, commit_implementation, kill_sandbox
    linear_activities.py  # LinearActivities.move_issue: muda o status da issue (por nome, no time dela)
    github_activities.py  # GitHubActivities: plan_exists, open_pull_request
    workflow.py       # PlanIssueWorkflow: In Planning → sandbox → clone → plano → commit → kill (finally) → In Progress
    implement_workflow.py # ImplementIssueWorkflow: plano existe? → sandbox → clone da branch → implementa →
                      # testes → push → kill (finally) → PR → In Review
    skills/writing-plans/  # SKILL.md + LICENSE vendorizados do superpowers
    worker.py         # entrypoint `software-factory-worker`
tests/                # unittest; payloads.py tem fixtures no formato do Linear
```

## Webhook do Linear

- Gatilho do planejamento: issue `create` com label `Feature` ou `Bug` (case-insensitive), ou
  `update` em que uma dessas labels foi adicionada (`updatedFrom.labelIds`).
- Gatilho da implementação: `update` com `stateId` em `updatedFrom` e `data.state.name ==
  "In Progress"` (case-insensitive), em issue com label de gatilho. Evento que adiciona a label
  **e** muda o status dispara só o planejamento.
- Eventos ignorados retornam 200 para o Linear não reenviar.
- `handle_issue` inicia o `PlanIssueWorkflow` com id `plan-issue-<issue.id>` (ou o
  `ImplementIssueWorkflow` com id `implement-issue-<issue.id>`); se já houver um rodando responde
  `{"status": "duplicate"}`; se o Temporal estiver fora, 503 (o Linear reenvia).
- O workflow hoje **gera o plano** com a skill `writing-plans` e o **commita** em
  `docs/superpowers/plans/<IDENTIFIER>.md` na branch `factory/<IDENTIFIER>` (push feito pela
  activity `commit_plan`; o Claude só tem permissão `Edit` nesse arquivo, sem `Bash`). Se a branch
  já existir no remoto com outro conteúdo, falha sem force push.
- O `ImplementIssueWorkflow` confere que a issue segue em `In Progress` (senão encerra), checa o
  plano via API do GitHub antes de criar sandbox (sem plano → move para `In Planning`), clona
  `factory/<IDENTIFIER>` com o token **fora** do `origin`, roda o Claude com `Bash`
  (`implement_plan`, 30 min, sem retry), roda `TEST_COMMAND` (`run_tests`), e só com verde faz
  push (`commit_implementation`, sem force push; falha se o Claude não mudou nada), destrói o
  sandbox, abre a PR `factory/<ID>` → branch default (reaproveita PR aberta) e move para
  `In Review`. Testes vermelhos ou qualquer falha: sem PR, sem compensação, issue fica em
  `In Progress`.
- Status no Linear: o workflow começa movendo a issue `To Do → In Planning` (só se estiver em
  `To Do`; senão planeja sem mover) e, depois do commit do plano e do kill do sandbox, move para
  `In Progress`. Status procurados por nome (case-insensitive) no time da issue; status inexistente
  falha sem retry. Em falha não há compensação: a issue fica em `In Planning`.
- Não há varredura de issues existentes em TODO (projeto novo, sem backlog).

## Comandos

```bash
# setup
uv venv .venv && source .venv/bin/activate && uv pip install -e ".[dev]"

# servidor Temporal local (terminal separado) — gRPC localhost:7233, UI http://localhost:8233
temporal server start-dev

# worker do Temporal (lê .env; precisa de E2B_API_KEY, ANTHROPIC_API_KEY ou CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, TARGET_REPO, LINEAR_API_KEY, TEST_COMMAND)
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
