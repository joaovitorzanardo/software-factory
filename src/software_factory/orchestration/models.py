from dataclasses import dataclass

# Status do Linear procurados por nome (case-insensitive) no time da issue.
STATE_TODO = "To Do"
STATE_IN_PLANNING = "In Planning"
STATE_IN_PROGRESS = "In Progress"
STATE_IN_REVIEW = "In Review"
STATE_DONE = "Done"


@dataclass(frozen=True)
class RepoTarget:
    repo: str  # owner/repo
    test_command: str


@dataclass(frozen=True)
class RepoResolution:
    target: RepoTarget | None = None
    problem: str = ""  # motivo (pt-BR) de não ter resolvido o repo


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
    target: RepoTarget


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
    target: RepoTarget


@dataclass(frozen=True)
class CloneBranchRequest:
    sandbox_id: str
    identifier: str
    target: RepoTarget


@dataclass(frozen=True)
class CloneRepoRequest:
    sandbox_id: str
    target: RepoTarget


@dataclass(frozen=True)
class RunTestsRequest:
    sandbox_id: str
    target: RepoTarget


@dataclass(frozen=True)
class BranchRequest:
    identifier: str
    target: RepoTarget


@dataclass(frozen=True)
class ImplementRequest:
    sandbox_id: str
    issue: PlanIssueInput
    target: RepoTarget


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
    target: RepoTarget
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
    target: RepoTarget
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
    target: RepoTarget
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class CommentRequest:
    issue_id: str
    # UUID gerado pelo chamador: idempotência (post_comment cria uma vez; upsert_comment edita).
    comment_id: str
    body: str


@dataclass(frozen=True)
class UsageReportRequest:
    issue_id: str
    # Com final, o comentário inclui a linha de total.
    final: bool = False


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
OUTCOME_MERGED = "merged"
OUTCOME_NO_PR = "no_pr"


@dataclass(frozen=True)
class ImplementResult:
    identifier: str
    outcome: str
    pr_url: str | None = None


REVIEW_DECISIONS = ("fix", "issue", "note", "reject", "ask")
REVIEW_SEVERITIES = ("critical", "important", "minor")
REVIEW_LENSES = ("correctness", "security")


@dataclass(frozen=True)
class PullInfo:
    number: int
    url: str
    head_sha: str
    base_ref: str
    # Merge-base entre base_ref e head (compare API).
    base_sha: str


@dataclass(frozen=True)
class ReviewFinding:
    lens: str
    severity: str
    file: str
    line: int | None
    title: str
    detail: str
    decision: str
    reason: str


@dataclass(frozen=True)
class ReviewRound:
    number: int
    summary: str
    findings: tuple[ReviewFinding, ...] = ()
    # Resumo da sessão de correção (o que corrigiu e o que não, e por quê).
    fix_notes: str = ""


@dataclass(frozen=True)
class ReviewRequest:
    sandbox_id: str
    issue: PlanIssueInput
    base_sha: str
    target: RepoTarget
    history: tuple[ReviewRound, ...] = ()
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class ReviewReport:
    summary: str
    findings: tuple[ReviewFinding, ...]
    # Preenchido pela activity a partir do diff (não pelo LLM): exige confirmação do merge.
    sensitive_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewFixRequest:
    sandbox_id: str
    issue: PlanIssueInput
    round: int
    findings: tuple[ReviewFinding, ...]
    target: RepoTarget
    clarifications: tuple[Clarification, ...] = ()


@dataclass(frozen=True)
class MergeRequest:
    identifier: str
    title: str
    number: int
    head_sha: str
    target: RepoTarget


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    head_changed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CreateIssueRequest:
    origin_issue_id: str
    # UUID v4 gerado pelo workflow.
    new_issue_id: str
    title: str
    description: str


@dataclass(frozen=True)
class FollowUpIssue:
    identifier: str
    url: str


@dataclass(frozen=True)
class ReviewResult:
    identifier: str
    outcome: str
    pr_url: str | None = None
    # Identifiers das issues criadas.
    follow_ups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileInput:
    # Labels que disparam a factory (as mesmas do webhook).
    trigger_labels: tuple[str, ...]


@dataclass(frozen=True)
class PendingIssue:
    # KIND_PLAN, KIND_IMPLEMENT ou KIND_REVIEW (dispatch.py).
    kind: str
    issue: PlanIssueInput
    # ISO 8601 UTC, como vem do Linear: quando o evento que dispara a etapa aconteceu.
    triggered_at: str


@dataclass(frozen=True)
class ReconcileResult:
    # Workflow ids iniciados.
    started: list[str]
    skipped: int


@dataclass(frozen=True)
class UsageAlert:
    """Janela de uso da assinatura do Claude que passou do limiar de aviso."""

    window: str
    utilization: float
    resets_at: int  # epoch em segundos
    source: str = ""  # <workflow_id>/<activity> que detectou
