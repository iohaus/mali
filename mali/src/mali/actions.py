"""Typed tutoring intents proposed by product surfaces and the engine."""

from dataclasses import dataclass
from enum import StrEnum

from mali.curriculum import Curriculum
from mali.ids import LearnerId, QuestionId, SkillCode
from mali.policy import TutorPolicy


class Actor(StrEnum):
    """The principal requesting a tutoring action."""

    ENGINE = "engine"
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    TEACHER = "teacher"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class StartPlacement:
    pass


@dataclass(frozen=True, slots=True)
class AskQuestion:
    skill: SkillCode
    seed: int


@dataclass(frozen=True, slots=True)
class RecordAnswer:
    question: QuestionId
    raw: str


@dataclass(frozen=True, slots=True)
class CertifyPlacement:
    pass


@dataclass(frozen=True, slots=True)
class ProposeTarget:
    skill: SkillCode


@dataclass(frozen=True, slots=True)
class ClearTarget:
    pass


@dataclass(frozen=True, slots=True)
class TeachEpisode:
    skill: SkillCode


@dataclass(frozen=True, slots=True)
class StartCheck:
    pass


@dataclass(frozen=True, slots=True)
class PassCheck:
    pass


@dataclass(frozen=True, slots=True)
class FailCheck:
    pass


@dataclass(frozen=True, slots=True)
class CloseStale:
    pass


@dataclass(frozen=True, slots=True)
class OverrideMastery:
    skill: SkillCode
    note: str


@dataclass(frozen=True, slots=True)
class RegisterLearner:
    learner: LearnerId


@dataclass(frozen=True, slots=True)
class RemoveLearner:
    pass


@dataclass(frozen=True, slots=True)
class LoadCurriculum:
    curriculum: Curriculum


@dataclass(frozen=True, slots=True)
class AdoptPolicy:
    policy: TutorPolicy


type Action = (
    StartPlacement
    | AskQuestion
    | RecordAnswer
    | CertifyPlacement
    | ProposeTarget
    | ClearTarget
    | TeachEpisode
    | StartCheck
    | PassCheck
    | FailCheck
    | CloseStale
    | OverrideMastery
    | RegisterLearner
    | RemoveLearner
    | LoadCurriculum
    | AdoptPolicy
)
