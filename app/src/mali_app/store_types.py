"""Typed results returned by the durable learner-record adapter."""

from dataclasses import dataclass
from enum import StrEnum

from mali.ids import LearnerId, SkillCode
from mali.plans import ActionPlan
from mali.rules import RefusalReason
from mali.snapshot import Snapshot


class ExecutionStatus(StrEnum):
    """The mutually exclusive outcomes of an attempted record update."""

    COMMITTED = "committed"
    REFUSED = "refused"
    STALE_RECORD = "stale_record"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The result of applying one core-planned action to the record."""

    status: ExecutionStatus
    snapshot: Snapshot | None
    plan: ActionPlan | None = None
    refusal: RefusalReason | None = None

    def __post_init__(self) -> None:
        if self.status is ExecutionStatus.COMMITTED:
            if self.snapshot is None or self.plan is None or self.refusal is not None:
                raise ValueError("a committed result needs a snapshot and plan")
        elif self.status is ExecutionStatus.REFUSED:
            if self.snapshot is None or self.refusal is None or self.plan is not None:
                raise ValueError("a refused result needs a snapshot and refusal")
        elif (
            self.snapshot is not None
            or self.plan is not None
            or self.refusal is not None
        ):
            raise ValueError("a stale record result carries no state")


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The outcome of comparing journal-derived and live progress."""

    valid: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TeachingTrace:
    """One completed, replay-excluded teaching turn retained for audit."""

    learner: str
    skill: SkillCode
    episode_id: str
    model: str
    prompt_version: str
    policy_version: str
    transcript: str
    tokens_in: int
    tokens_out: int
    episode_outcome: str


@dataclass(frozen=True, slots=True)
class TeacherLearnerSummary:
    """One learner row prepared for the teacher dashboard."""

    learner: LearnerId
    display_name: str
    mastered_count: int
    ready_count: int
    current_title: str | None
    evidence_count: int
    audit_valid: bool


@dataclass(frozen=True, slots=True)
class QuestionEvidence:
    """One question as shown and the recorded student response, if any."""

    prompt: str
    response: str | None
    correct: bool | None
    answered_at: str | None


@dataclass(frozen=True, slots=True)
class LearningClaim:
    """A teacher-facing claim paired with its recorded support."""

    title: str
    detail: str
    occurred_at: str
    attribution: str
    questions: tuple[QuestionEvidence, ...]


@dataclass(frozen=True, slots=True)
class TeacherLearnerDetail:
    """The complete teacher view for one learner."""

    learner: LearnerId
    display_name: str
    mastered: tuple[str, ...]
    next_up: tuple[str, ...]
    later: tuple[str, ...]
    audit: AuditResult
    claims: tuple[LearningClaim, ...]
