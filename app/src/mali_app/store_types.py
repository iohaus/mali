"""Typed results returned by the durable learner-record adapter."""

from dataclasses import dataclass
from enum import StrEnum

from mali.ids import SkillCode
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
