"""Opaque identifiers validated at the boundary of the tutoring core."""

from re import Pattern, compile
from typing import NewType

from mali.errors import InvalidIdentifier

LearnerId = NewType("LearnerId", str)
CheckPointId = NewType("CheckPointId", str)
QuestionId = NewType("QuestionId", str)
SkillCode = NewType("SkillCode", str)

_IDENTIFIER_PATTERN: Pattern[str] = compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SKILL_CODE_PATTERN: Pattern[str] = compile(r"^[a-z][a-z0-9-]{0,47}$")


def learner_id(value: object) -> LearnerId:
    """Create a validated learner identifier."""
    return LearnerId(_validate_identifier(value, "learner identifier"))


def checkpoint_id(value: object) -> CheckPointId:
    """Create a validated checkpoint identifier."""
    return CheckPointId(_validate_identifier(value, "checkpoint identifier"))


def question_id(value: object) -> QuestionId:
    """Create a validated question identifier."""
    return QuestionId(_validate_identifier(value, "question identifier"))


def skill_code(value: object) -> SkillCode:
    """Create a validated curriculum skill code."""
    if not isinstance(value, str) or _SKILL_CODE_PATTERN.fullmatch(value) is None:
        raise InvalidIdentifier("skill code must be lowercase kebab-case")
    return SkillCode(value)


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise InvalidIdentifier(f"{label} must contain 1 to 64 safe characters")
    return value
