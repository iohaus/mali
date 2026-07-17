"""Curriculum types that describe assessable tutoring skills."""

from dataclasses import dataclass

from mali.errors import InvalidIdentifier, InvalidSkill
from mali.ids import SkillCode, skill_code

_MAX_SKILL_TITLE_LENGTH = 120
_MAX_SKILL_CARD_LENGTH = 2_000
_MIN_BIT_INDEX = 0
_MAX_BIT_INDEX = 62


@dataclass(frozen=True, slots=True)
class Skill:
    """An atomic skill that Mali can teach and assess."""

    code: SkillCode
    bit_index: int
    title: str
    card: str

    def __post_init__(self) -> None:
        try:
            skill_code(self.code)
        except InvalidIdentifier as error:
            raise InvalidSkill("skill code must be lowercase kebab-case") from error
        if type(self.bit_index) is not int:
            raise InvalidSkill("skill bit index must be an integer")
        if not _MIN_BIT_INDEX <= self.bit_index <= _MAX_BIT_INDEX:
            raise InvalidSkill("skill bit index must be between 0 and 62")
        _validate_text(self.title, "skill title", _MAX_SKILL_TITLE_LENGTH)
        _validate_text(self.card, "skill card", _MAX_SKILL_CARD_LENGTH)


def _validate_text(value: object, label: str, maximum_length: int) -> None:
    if not isinstance(value, str):
        raise InvalidSkill(f"{label} must be text")
    if not value.strip():
        raise InvalidSkill(f"{label} must not be blank")
    if len(value) > maximum_length:
        raise InvalidSkill(f"{label} exceeds its maximum length")
