"""Immutable question records and assessment checkpoints."""

from dataclasses import dataclass
from enum import StrEnum

from mali.errors import InvalidCheckpoint
from mali.ids import CheckPointId, QuestionId, SkillCode
from mali.templates import QuestionInstance, canonical_answer


class CheckPointKind(StrEnum):
    """The purpose of an open question set."""

    PLACEMENT = "placement"
    CHECK = "check"


@dataclass(frozen=True, slots=True)
class Answer:
    """A normalized learner answer and its computed verdict."""

    value: str
    correct: bool


@dataclass(frozen=True, slots=True)
class Question:
    """A presented question with an optional machine-checked answer."""

    identifier: QuestionId
    skill: SkillCode
    instance: QuestionInstance
    answer: Answer | None = None

    def __post_init__(self) -> None:
        if type(self.identifier) is not str or not self.identifier:
            raise InvalidCheckpoint("question identifier must be validated")
        if type(self.skill) is not str or not self.skill:
            raise InvalidCheckpoint("question skill must be validated")
        if self.answer is not None:
            normalized = canonical_answer(self.instance.answer_type, self.answer.value)
            if normalized is None:
                raise InvalidCheckpoint("question answer is not readable")
            if self.answer.correct != (normalized == self.instance.key):
                raise InvalidCheckpoint(
                    "question verdict does not match its answer key"
                )


@dataclass(frozen=True, slots=True)
class CheckPoint:
    """A bounded placement or mastery-check question set."""

    identifier: CheckPointId
    kind: CheckPointKind
    target: SkillCode | None
    questions: tuple[Question, ...]

    def __post_init__(self) -> None:
        if type(self.identifier) is not str or not self.identifier:
            raise InvalidCheckpoint("checkpoint identifier must be validated")
        if self.kind is CheckPointKind.PLACEMENT and self.target is not None:
            raise InvalidCheckpoint("placement checkpoints do not have a target")
        if self.kind is CheckPointKind.CHECK and self.target is None:
            raise InvalidCheckpoint("mastery checks need a target")
        if self.kind is CheckPointKind.CHECK and any(
            question.skill != self.target for question in self.questions
        ):
            raise InvalidCheckpoint("mastery check questions must match their target")
        identifiers = tuple(question.identifier for question in self.questions)
        if len(set(identifiers)) != len(identifiers):
            raise InvalidCheckpoint("checkpoint question identifiers must be unique")


def has_passed(questions: tuple[Question, ...], needed: int) -> bool:
    """Return whether a check has enough correct answers to pass."""
    if type(needed) is not int or needed < 1:
        raise InvalidCheckpoint("required correct answers must be positive")
    return (
        sum(
            question.answer is not None and question.answer.correct
            for question in questions
        )
        >= needed
    )


def cannot_pass(questions: tuple[Question, ...], needed: int, limit: int) -> bool:
    """Return whether the remaining questions cannot satisfy a pass rule."""
    if type(limit) is not int or limit < needed:
        raise InvalidCheckpoint("question limit must cover the pass requirement")
    answered = sum(question.answer is not None for question in questions)
    correct = sum(
        question.answer is not None and question.answer.correct
        for question in questions
    )
    return correct + (limit - answered) < needed
