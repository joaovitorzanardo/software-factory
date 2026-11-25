"""Prompts da review automática (lentes, triagem e correção) e o parser do review.json."""

import json
from typing import Any

from temporalio.exceptions import ApplicationError

from software_factory.orchestration.activities import (
    SKILLS_DEST_DIR,
    _clarifications_section,
    _fence,
    _issue_section,
    _NO_TEST_TAMPERING,
    plan_path,
)
from software_factory.orchestration.models import (
    REVIEW_DECISIONS,
    REVIEW_LENSES,
    REVIEW_SEVERITIES,
    ReviewFinding,
    ReviewFixRequest,
    ReviewReport,
    ReviewRequest,
    ReviewRound,
)

# Arquivos da review fora do repo (nunca commitados).
REVIEW_DIR = "/home/user/review"
CORRECTNESS_PATH = f"{REVIEW_DIR}/correctness.md"
SECURITY_PATH = f"{REVIEW_DIR}/security.md"
REVIEW_JSON_PATH = f"{REVIEW_DIR}/review.json"
FINDING_DETAIL_CAP = 1000
# Arquivos que mudam o comportamento do agente, da CI ou dos testes: o código revisado pode
# controlar o revisor por eles, então a aprovação deles pede confirmação humana antes do merge.
# Terminado em "/" = diretório (em qualquer nível); senão, nome de arquivo em qualquer diretório.
SENSITIVE_PATTERNS = (
    "CLAUDE.md",
    ".claude/",
    ".github/workflows/",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
    ".pre-commit-config.yaml",
    "Makefile",
    ".factory.toml",
)

_FINDING_FIELDS = ("lens", "severity", "file", "line", "title", "detail", "decision", "reason")

_READ_ONLY = (
    "A review é só leitura: não edite arquivos do repo, não faça commit, checkout, stash nem "
    "nada que mude o working tree, o índice ou o HEAD. Use `git diff`, `git show` e `git log`."
)

_TRIAGE_RULES = """- `fix`: o problema foi confirmado no código, é `critical` ou `important` e foi
  introduzido pela PR ou está no escopo da issue. Bloqueia o merge.
- `issue`: é real, mas fica fora do escopo da issue, é código antigo que a PR não tocou ou o
  custo de corrigir agora não compensa, e mesmo assim merece acompanhamento. Vira uma issue nova
  no Linear.
- `note`: é real e pequeno (`minor`). Só aparece no comentário de resumo da rodada.
- `reject`: é falso positivo ou está errado. Registre a razão técnica em `reason`.
- `ask`: só para decisão grande (mudar o escopo ou os requisitos da issue, mudar a arquitetura do
  plano, mudar o comportamento esperado por testes existentes, adicionar dependência, migração de
  dados). Vira pergunta no Linear.
- Achado `minor` nunca vira `fix`.
- Achado de `security` `critical` nunca vira `note` nem `issue`: ou `fix`, ou `ask`, ou `reject`
  com a prova em `reason`."""


def _is_sensitive(path: str) -> bool:
    for pattern in SENSITIVE_PATTERNS:
        if pattern.endswith("/"):
            if path.startswith(pattern) or f"/{pattern}" in path:
                return True
        elif path == pattern or path.endswith(f"/{pattern}"):
            return True
    return False


def sensitive_files(paths: list[str]) -> tuple[str, ...]:
    """Arquivos do diff que casam com SENSITIVE_PATTERNS, na ordem do diff."""
    return tuple(p for p in paths if _is_sensitive(p))


def _review_context(request: ReviewRequest) -> str:
    return f"{_clarifications_section(request.clarifications)}{_issue_section(request.issue)}"


def build_correctness_prompt(request: ReviewRequest) -> str:
    template = f"{SKILLS_DEST_DIR}/requesting-code-review/code-reviewer.md"
    plan = plan_path(request.issue.identifier)
    return f"""Faça a code review da PR da issue abaixo (lente de corretude).

1. Leia `{template}` e faça **você mesmo** a review descrita no prompt do template, **sem
   despachar subagente**. Preencha os placeholders assim:
   - DESCRIPTION: a issue {request.issue.identifier} abaixo.
   - PLAN_OR_REQUIREMENTS: o plano em `{plan}` e a issue abaixo.
   - BASE_SHA: `{request.base_sha}`
   - HEAD_SHA: `HEAD`
2. {_READ_ONLY}
3. Salve o relatório completo, no formato de saída do template (Critical / Important / Minor,
   cada item com `arquivo:linha`), em `{CORRECTNESS_PATH}`. Não escreva em nenhum outro lugar.
4. Não faça perguntas; termine com um resumo curto.

{_review_context(request)}"""


def build_security_prompt(request: ReviewRequest) -> str:
    return f"""Faça a review de segurança da PR da issue abaixo.

1. Revise o diff `git diff {request.base_sha}..HEAD` (e o código ao redor, quando
   precisar). Checklist:
   - injeção (shell, SQL, template);
   - segredos em código ou em log;
   - validação de entrada vinda de fora (webhook, HTTP, arquivo, variável de ambiente);
   - path traversal;
   - SSRF;
   - desserialização insegura;
   - autenticação e autorização;
   - dependência nova (necessária? confiável? versão fixada?);
   - testes apagados, pulados ou afrouxados (`skip`, `xfail`, asserts removidos, comando de
     testes alterado).
2. {_READ_ONLY}
3. Salve o relatório em `{SECURITY_PATH}`, com as seções Critical / Important / Minor e, em cada
   item, `arquivo:linha`, o que está errado, por que importa e como corrigir. Seção sem achados
   fica com "nenhum". Não escreva em nenhum outro lugar.
4. Não faça perguntas; termine com um resumo curto.

{_review_context(request)}"""


def _finding_line(f: ReviewFinding) -> str:
    # Título, detalhe, motivo e resumo vêm do LLM (e do código revisado): sempre cercados.
    where = f"{f.file}:{f.line}" if f.line is not None else f.file
    return f"[{f.lens}/{f.severity}] `{where}`, título:\n\n{_fence(f.title)}"


def _history_section(history: tuple[ReviewRound, ...]) -> str:
    if not history:
        return ""
    rounds = []
    for r in history:
        items = "\n\n".join(
            f"- {_finding_line(f)}\n\n  Decisão: `{f.decision}`, motivo:\n\n{_fence(f.reason)}"
            for f in r.findings
        )
        notes = f"\n\nResumo da correção:\n\n{_fence(r.fix_notes)}" if r.fix_notes else ""
        rounds.append(
            f"### Rodada {r.number}\n\nResumo:\n\n{_fence(r.summary)}\n\n"
            f"{items or '- (sem achados)'}{notes}"
        )
    return "## Rodadas anteriores\n\n" + "\n\n".join(rounds) + "\n\n"


def build_triage_prompt(request: ReviewRequest) -> str:
    return f"""Faça a triagem dos relatórios de review da PR da issue abaixo.

1. Use a skill `receiving-code-review`. Leia `{CORRECTNESS_PATH}` (lente `correctness`) e
   `{SECURITY_PATH}` (lente `security`).
2. **Confira cada item no código** (diff `git diff {request.base_sha}..HEAD`) antes de decidir;
   não aceite nem rejeite sem verificar. {_READ_ONLY}
3. Decida cada achado com estas regras:

{_TRIAGE_RULES}

4. Não repita achados das rodadas anteriores que já têm decisão, salvo evidência nova. Achados
   `fix` de rodadas anteriores que continuam abertos no código entram de novo, como `fix`.
5. Respeite as decisões da pessoa listadas abaixo, se houver.
6. Grave `{REVIEW_JSON_PATH}` com JSON válido, exatamente neste schema:

```json
{{"summary": "resumo da rodada",
  "findings": [{{"lens": "correctness|security", "severity": "critical|important|minor",
                "file": "caminho/no/repo", "line": 12, "title": "título curto",
                "detail": "o problema e como corrigir", "decision": "fix|issue|note|reject|ask",
                "reason": "por que essa decisão"}}]}}
```

   `line` pode ser `null`. Sem achados, `findings` é `[]`. Num achado `ask`, `detail` traz a
   pergunta, as opções e a sua recomendação. `detail` e `reason` com até {FINDING_DETAIL_CAP}
   caracteres.
7. Não faça perguntas interativas; termine com um resumo curto.

{_history_section(request.history)}{_review_context(request)}"""


def build_review_fix_prompt(request: ReviewFixRequest, test_command: str) -> str:
    items = "\n\n".join(
        f"### Achado {i}: {_finding_line(f)}\n\nDetalhe:\n\n{_fence(f.detail)}\n\n"
        f"Por que corrigir:\n\n{_fence(f.reason)}"
        for i, f in enumerate(request.findings, start=1)
    )
    return f"""Corrija os achados da review (rodada {request.round}) da PR da issue abaixo.

- Corrija **só** os achados listados, um de cada vez, com TDD (teste primeiro, ver falhar,
  corrigir, ver passar).
- Use a skill `receiving-code-review`: confira cada achado no código antes de mudar. Se um
  achado se mostrar errado ao implementar, não o corrija e explique o motivo técnico no resumo
  final.
- Os testes do repo rodam com `{test_command}`; rode até ficar verde antes de terminar.
- {_NO_TEST_TAMPERING}
- Faça commits locais. **Não** faça `git push` nem mexa em remotes; o push é feito depois, fora
  daqui.
- Não faça perguntas; termine com um resumo que diga, achado por achado, o que foi feito (ou por
  que não foi).

## Achados a corrigir

{items}

{_clarifications_section(request.clarifications)}{_issue_section(request.issue)}"""


def _invalid(message: str) -> ApplicationError:
    return ApplicationError(f"review.json inválido: {message}", non_retryable=True)


def _parse_finding(index: int, raw: Any) -> ReviewFinding:
    if not isinstance(raw, dict):
        raise _invalid(f"achado {index} não é objeto")
    missing = [name for name in _FINDING_FIELDS if name not in raw]
    if missing:
        raise _invalid(f"achado {index} sem {', '.join(missing)}")
    for name in ("file", "title", "detail", "reason"):
        if not isinstance(raw[name], str):
            raise _invalid(f"achado {index}: {name} não é texto")
    for name, allowed in (
        ("lens", REVIEW_LENSES),
        ("severity", REVIEW_SEVERITIES),
        ("decision", REVIEW_DECISIONS),
    ):
        if raw[name] not in allowed:
            raise _invalid(f"achado {index}: {name} {raw[name]!r} fora de {allowed}")
    line = raw["line"]
    if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
        raise _invalid(f"achado {index}: line {line!r} não é inteiro nem null")
    if raw["severity"] == "minor" and raw["decision"] == "fix":
        raise _invalid(f"achado {index}: minor não pode ser fix")
    if (
        raw["lens"] == "security"
        and raw["severity"] == "critical"
        and raw["decision"] in ("note", "issue")
    ):
        raise _invalid(f"achado {index}: security critical não pode ser {raw['decision']}")
    return ReviewFinding(
        lens=raw["lens"],
        severity=raw["severity"],
        file=raw["file"],
        line=line,
        title=raw["title"],
        detail=raw["detail"][:FINDING_DETAIL_CAP],
        decision=raw["decision"],
        reason=raw["reason"][:FINDING_DETAIL_CAP],
    )


def parse_review(text: str) -> ReviewReport:
    """Valida o review.json da triagem; qualquer desvio do schema ou das regras falha sem retry."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise _invalid(f"JSON malformado ({error})") from None
    if not isinstance(data, dict):
        raise _invalid("raiz não é objeto")
    summary = data.get("summary")
    findings = data.get("findings")
    if not isinstance(summary, str):
        raise _invalid("summary ausente ou não é texto")
    if not isinstance(findings, list):
        raise _invalid("findings ausente ou não é lista")
    return ReviewReport(
        summary=summary,
        findings=tuple(_parse_finding(i, raw) for i, raw in enumerate(findings, start=1)),
    )
