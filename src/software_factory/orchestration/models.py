from dataclasses import dataclass

# Status do Linear procurados por nome (case-insensitive) no time da issue.
STATE_TODO = "To Do"
STATE_IN_PLANNING = "In Planning"
STATE_IN_PROGRESS = "In Progress"
STATE_IN_REVIEW = "In Review"


@dataclass(frozen=True)
class PlanIssueInput:
    issue_id: str
    identifier: str
    title: str
    description: str | None
    url: str | None


@dataclass(frozen=True)
class PlanRequest:
    sandbox_id: str
    issue: PlanIssueInput


@dataclass(frozen=True)
class CommitPlanRequest:
    sandbox_id: str
    identifier: str


@dataclass(frozen=True)
class PlanResult:
    identifier: str
    plan: str
    sandbox_id: str
    branch: str


@dataclass(frozen=True)
class MoveIssueRequest:
    issue_id: str
    to_state: str
    # Guarda: só move se o status atual for este.
    from_state: str | None = None
    # Status que precisam existir no time, validados antes de mover ou pular (falha cedo).
    require_states: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequestRequest:
    identifier: str
    title: str
    url: str | None


@dataclass(frozen=True)
class CloneBranchRequest:
    sandbox_id: str
    identifier: str


@dataclass(frozen=True)
class ImplementRequest:
    sandbox_id: str
    issue: PlanIssueInput


@dataclass(frozen=True)
class TestRunResult:
    passed: bool
    # Fim da saída (stdout + stderr) do comando de testes.
    output: str


@dataclass(frozen=True)
class CommitImplementationRequest:
    sandbox_id: str
    identifier: str
    # Commit clonado de factory/<ID>: HEAD igual a ele = Claude não mudou nada.
    base_sha: str
    # Vazio: "feat: implementa <ID>".
    message: str = ""
    # False: sem mudanças devolve a branch sem push (checkpoint opcional).
    require_changes: bool = True


@dataclass(frozen=True)
class FixAttempt:
    number: int
    analysis: str
    # Saída dos testes depois da correção.
    test_output: str


@dataclass(frozen=True)
class Clarification:
    question: str
    answer: str


@dataclass(frozen=True)
class AnalyzeRequest:
    sandbox_id: str
    issue: PlanIssueInput
    test_output: str
    attempts: tuple[FixAttempt, ...] = ()
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class FixAnalysis:
    analysis: str
    plan: str | None = None
    # Preenchida = decisão grande; plan fica None.
    question: str | None = None


@dataclass(frozen=True)
class FixRequest:
    sandbox_id: str
    issue: PlanIssueInput
    analysis: str
    plan: str
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class CommentRequest:
    issue_id: str
    # UUID v4 gerado no workflow: idempotência.
    comment_id: str
    body: str


@dataclass(frozen=True)
class HumanAnswer:
    # Id do comentário da pergunta (parentId do reply).
    question_id: str
    comment_id: str
    body: str


OUTCOME_PR_OPENED = "pr_opened"
OUTCOME_NO_PLAN = "no_plan"
OUTCOME_SKIPPED = "skipped"
OUTCOME_STOPPED = "stopped"


@dataclass(frozen=True)
class ImplementResult:
    identifier: str
    outcome: str
    pr_url: str | None = None
