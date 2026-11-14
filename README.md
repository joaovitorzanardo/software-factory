# Software Factory

## O que é

Uma "fábrica de software" experimental: a ideia é que uma tarefa escrita no Linear vire código
pronto para revisão sem que ninguém precise programar à mão.

O caminho completo que queremos automatizar é:

1. alguém cria uma issue no Linear descrevendo uma funcionalidade ou um bug;
2. a fábrica escreve um **plano** de como resolver;
3. a fábrica **implementa** o plano;
4. a fábrica abre um **Pull Request** no GitHub, revisa o código, corrige o que for preciso e faz o merge.

Hoje (fase de teste) a fábrica faz as quatro etapas: ela recebe a issue, escreve o plano e salva
esse plano numa branch do repositório; quando a issue entra em `In Progress`, implementa o plano,
roda os testes e, se ficarem vermelhos, entra num loop de análise e correção até ficarem verdes;
só então abre o PR e move a issue para `In Review`. Dúvidas grandes viram comentário na issue, e a
resposta é um reply nesse comentário.

Quem faz o trabalho "inteligente" é o **Claude Code**, rodando dentro de uma máquina isolada e
descartável (sandbox do **E2B**). Quem coordena os passos, tenta de novo quando algo falha e
garante que nada fique pela metade é o **Temporal**.

## Como funciona hoje

```mermaid
flowchart TD
    A([Pessoa cria ou edita issue no Linear<br/>com label Feature ou Bug]) --> B[Linear envia webhook]
    B --> C{Servidor de webhooks<br/>assinatura válida e<br/>label Feature/Bug?}
    C -- não --> C1([Ignora e responde 200])
    C -- "label adicionada<br/>ou issue criada" --> D{Já existe workflow<br/>para essa issue?}
    C -- "status mudou para<br/>In Progress" --> P{Já existe workflow<br/>de implementação?}
    D -- sim --> D1([Responde duplicate])
    D -- não --> E[Inicia PlanIssueWorkflow<br/>no Temporal]
    P -- sim --> D1
    P -- não --> Q[Inicia ImplementIssueWorkflow<br/>no Temporal]

    subgraph W[Worker do Temporal: planejamento]
        E --> F{Issue está em To Do?}
        F -- sim --> F1[Move a issue para In Planning]
        F -- não --> G
        F1 --> G[Cria sandbox E2B]
        G --> H[Clona o repositório alvo]
        H --> I[Claude Code escreve o plano<br/>com a skill writing-plans]
        I --> J[Commit e push do plano na<br/>branch factory/IDENTIFIER]
        J --> K[Destrói o sandbox<br/>sempre, mesmo em falha]
        K --> L[Move a issue para In Progress]
    end

    L -. webhook de status .-> B

    subgraph X[Worker do Temporal: implementação]
        Q --> R{Issue ainda em<br/>In Progress?}
        R -- não --> R1([Encerra sem fazer nada])
        R -- sim --> S{Plano existe em<br/>factory/IDENTIFIER?}
        S -- não --> S1([Move a issue de volta<br/>para In Planning])
        S -- sim --> T[Cria sandbox E2B]
        T --> U[Clona a branch factory/IDENTIFIER]
        U --> V[Claude Code implementa o plano]
        V --> Y{Testes verdes?}
        Y -- não --> Y1[Checkpoint wip na branch;<br/>Claude analisa e corrige<br/>em novos sandboxes]
        Y1 -. pergunta no Linear,<br/>reply da pessoa .-> Y1
        Y1 --> Y
        Y -- sim --> Z[Commit e push na branch]
        Z --> Z1[Destrói o sandbox<br/>sempre, mesmo em falha]
        Z1 --> Z2[Abre o Pull Request]
        Z2 --> Z3([Move a issue para In Review])
    end
```

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Temporal CLI](https://docs.temporal.io/develop/python/set-up-your-local-python)

## Setup

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env   # preencha os segredos (ver abaixo)
```

## Temporal (servidor local)

Em um terminal separado:

```bash
temporal server start-dev
```

- Temporal Service: `localhost:7233`
- Web UI: http://localhost:8233

## Variáveis de ambiente

| Variável | Usada por | Descrição |
| --- | --- | --- |
| `LINEAR_WEBHOOK_SECRET` | webhook | signing secret do webhook do Linear |
| `HOST` / `PORT` | webhook | bind do servidor (default `0.0.0.0:8000`) |
| `TEMPORAL_ADDRESS` | ambos | default `localhost:7233` |
| `TASK_QUEUE` | ambos | default `software-factory` |
| `E2B_API_KEY` | worker | chave da API do E2B |
| `E2B_TEMPLATE` | worker | template do sandbox (default `claude`) |
| `ANTHROPIC_API_KEY` | worker | auth do Claude Code via API (cobrança por token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | worker | alternativa: token da assinatura Claude (`claude setup-token`); defina só uma das duas |
| `GITHUB_TOKEN` | worker | token fine-grained restrito ao repo alvo, com **Contents: Read and write** e **Pull requests: Read and write** |
| `TARGET_REPO` | worker | repo que a fábrica opera (`owner/repo`) |
| `TEST_COMMAND` | worker | comando que roda os testes do repo alvo no sandbox (exit 0 = verde), ex.: `uv pip install --system -e ".[dev]" && python -m pytest` |
| `LINEAR_API_KEY` | worker | personal API key do Linear, com escrita em issues (muda o status) |

## Worker (Temporal + E2B)

```bash
software-factory-worker
```

Para cada issue aceita pelo webhook, o `PlanIssueWorkflow` (id `plan-issue-<issue id>`):

1. move a issue de `To Do` para `In Planning` no Linear (se ela não estiver em `To Do`, não move
   e segue planejando);
2. cria um sandbox E2B (template `claude`);
3. clona `TARGET_REPO` em `/home/user/repo`;
4. roda `claude -p` com a skill `writing-plans` (superpowers, vendorizada) para escrever o plano em
   `docs/superpowers/plans/<IDENTIFIER>.md` — o Claude só pode ler o repo e escrever esse arquivo;
5. commita o plano e faz push na branch `factory/<IDENTIFIER>`;
6. destrói o sandbox (sempre, inclusive em falha);
7. move a issue para `In Progress`.

Os status `To Do`, `In Planning` e `In Progress` precisam existir no time da issue (o nome é
comparado sem diferenciar maiúsculas). Em falha a issue fica no status em que estava, normalmente
`In Planning`. A API do Linear é só GraphQL (`https://api.linear.app/graphql`), chamada com `httpx`.

O plano e a branch ficam no resultado do workflow, na UI do Temporal (http://localhost:8233).

Quando a issue entra em `In Progress` (movida pelo `PlanIssueWorkflow` ou por uma pessoa), o
`ImplementIssueWorkflow` (id `implement-issue-<issue id>`):

1. confere que a issue segue em `In Progress` (senão encerra sem fazer nada) e que os status
   `In Review` e `In Planning` existem no time;
2. checa pela API do GitHub se o plano existe em `factory/<IDENTIFIER>`; se não existe, move a
   issue de volta para `In Planning` e encerra, sem criar sandbox;
3. cria um sandbox E2B e clona a branch `factory/<IDENTIFIER>` (últimos 50 commits) — o `origin` fica sem o token
   (o Claude roda com `Bash` liberado; isso dificulta, mas não impede, que ele chegue ao token,
   já que o push depois roda no mesmo sandbox);
4. roda `claude -p` para implementar o plano (até 30 min, sem retry);
5. roda `TEST_COMMAND` no repo (o orquestrador não confia no relato do Claude); estourar o timeout
   de 10 min conta como vermelho;
6. com verde, commita o que faltar e faz push na branch (o token só entra no ambiente desse
   comando; commit e push rodam com os hooks do repo desligados); o sandbox é destruído (sempre, inclusive em falha);
7. abre o PR `factory/<IDENTIFIER>` → branch default pela API REST do GitHub (reaproveita um PR
   já aberto) e move a issue para `In Review`.

Com testes vermelhos, o workflow entra no **loop de correção**: faz push de um checkpoint
`wip: tentativa <n> de <IDENTIFIER> (testes vermelhos)` na branch (a PR só abre com verde) e repete,
cada passo em seu próprio sandbox, analisar a falha (skill `systematic-debugging`, vendorizada),
planejar e corrigir, até os testes ficarem verdes. Uma sessão de análise ou de correção que falha
(erro ou timeout do Claude) não encerra o loop: vira mais uma tentativa vermelha. Se o Claude tiver uma dúvida que só uma pessoa
resolve, ou depois de 5 correções seguidas sem verde, o workflow posta uma pergunta como
comentário na issue e espera; **responda com um reply na thread desse comentário** (sem resposta em
7 dias o workflow falha). Para parar o loop, mova a issue para fora de `In Progress` (o status é
conferido antes de cada tentativa e a cada hora durante a espera); nesse caso termina sem PR.
Qualquer outra falha (sandbox, clone, push, a implementação inicial) deixa a issue em `In Progress`.

Quando a issue entra em `In Review` (movida pelo `ImplementIssueWorkflow` ou por uma pessoa), o
`ReviewIssueWorkflow` (id `review-issue-<issue id>`) revisa a PR `factory/<IDENTIFIER>`:

1. cria um sandbox e roda, **em paralelo**, duas lentes só de leitura sobre o diff inteiro da PR:
   `correctness` (skill `requesting-code-review`) e `security`; depois uma triagem com a skill
   `receiving-code-review`, que decide cada achado: `fix`, `issue`, `note`, `reject` ou `ask`;
2. achado `minor` nunca vira `fix` (só `note`, `issue` ou `reject`), então o loop converge;
3. `issue` cria uma issue de follow-up no Linear **sem** label de gatilho (a pessoa adiciona
   `Feature`/`Bug` se quiser que a fábrica pegue), sem repetir a mesma issue entre rodadas;
4. cada rodada posta um comentário de resumo na issue;
5. com `fix`, uma sessão de correção roda os testes, faz push e começa nova rodada;
6. sem `fix`, faz o merge **squash** da PR usando o sha revisado (se o head mudou, revisa de novo)
   e move a issue para `Done`. A branch `factory/<IDENTIFIER>` não é apagada.

Depois de 3 rodadas (`MAX_REVIEW_ROUNDS`) sem aprovação, ou se a PR não puder ser mergeada
(conflito, branch protection), o workflow pergunta no Linear (**reply na thread**, 7 dias). Para
parar, mova a issue para fora de `In Review`.

Requisitos e ressalvas da review:

- O time do Linear precisa do status `Done`.
- O token do GitHub precisa poder fazer merge: **Contents** e **Pull requests: Read and write**,
  que já são necessários.
- Branch protection que exige aprovação **bloqueia o merge**, porque o autor da PR é o dono do
  token. Nesse caso o workflow pergunta; aprove ou ajuste a regra.
- Sugestão: ative "Automatically delete head branches" no repo alvo, já que a fábrica não apaga
  `factory/<IDENTIFIER>`.
- Ainda não validado em sandbox E2B real: os dois processos `claude` das lentes rodam ao mesmo
  tempo e compartilham `~/.claude`.

## Executar (servidor de webhooks)

```bash
software-factory          # lê .env; sobe em HOST:PORT (default 0.0.0.0:8000)
# ou
uvicorn software_factory.api:create_app --factory --env-file .env --reload
```

Endpoints:

- `GET /health` → `{"status": "ok"}`
- `POST /webhooks/linear` → recebe webhooks do Linear

## Webhook do Linear

O Linear exige uma URL HTTPS pública. Localmente, exponha a porta com um túnel:

```bash
ngrok http --url=<seu-dominio>.ngrok-free.app 8000
# ou
cloudflared tunnel --url http://localhost:8000
```

No Linear: **Settings → API → Webhooks → New webhook**

- URL: `https://<túnel>/webhooks/linear`
- Resource types: **Issues** e **Comments** (os replies às perguntas do loop chegam como
  comentários)
- Copie o *signing secret* para `LINEAR_WEBHOOK_SECRET` no `.env`

O endpoint valida a assinatura (`Linear-Signature`, 401 se inválida) e o
`webhookTimestamp` (400 se tiver mais de 60s). Com label `Feature`/`Bug`, issues **criadas**
ou **atualizadas** recebendo uma dessas labels iniciam o planejamento, e issues cujo status
**mudou para `In Progress`** iniciam a implementação e para **`In Review`** iniciam a review (se o mesmo evento adiciona a label e muda
o status, vale só o planejamento). Comentários criados como **reply** numa thread viram o signal `human_answer` do workflow de
implementação ou de review da issue (`accepted`, ou `ignored` se não houver um rodando). Os demais eventos
retornam 200 `{"status": "ignored"}`.
Issues aceitas iniciam o workflow (`{"status": "accepted"}`, ou `"duplicate"` se já houver um
rodando para a issue);
se o Temporal estiver indisponível, responde 503 para o Linear reenviar.

## Testes

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
