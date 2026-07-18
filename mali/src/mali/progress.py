"""Immutable learner progress validated against a curriculum."""

from dataclasses import dataclass

from mali.curriculum import Curriculum
from mali.errors import InvalidProgress
from mali.ids import LearnerId, SkillCode


@dataclass(frozen=True, slots=True)
class Progress:
    """The durable progress Mali can make claims about."""

    learner: LearnerId
    curriculum_version: str
    mask: int
    placed: bool
    target: SkillCode | None
    version: int
    curriculum: Curriculum

    def __post_init__(self) -> None:
        if type(self.learner) is not str or not self.learner:
            raise InvalidProgress("learner identifier must be validated")
        if self.curriculum_version != self.curriculum.version:
            raise InvalidProgress("progress must use its curriculum version")
        if not self.curriculum.is_reachable(self.mask):
            raise InvalidProgress("progress mask is not valid for this curriculum")
        if type(self.version) is not int or self.version < 0:
            raise InvalidProgress("progress version must be a non-negative integer")
        if self.target is not None:
            if not self.placed:
                raise InvalidProgress("a learner needs placement before a study target")
            if self.target not in {
                skill.code for skill in self.curriculum.next_up(self.mask)
            }:
                raise InvalidProgress("study target is not ready for this learner")
