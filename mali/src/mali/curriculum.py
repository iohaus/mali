"""Curriculum types that describe assessable tutoring skills."""

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from mali.errors import (
    CurriculumTooLarge,
    DuplicateRequirement,
    DuplicateSkill,
    InvalidIdentifier,
    InvalidProgress,
    InvalidSkill,
    PrerequisiteCycle,
    SelfRequirement,
    UnknownSkill,
)
from mali.ids import SkillCode, skill_code

_MAX_SKILL_TITLE_LENGTH = 120
_MAX_SKILL_CARD_LENGTH = 2_000
_MIN_BIT_INDEX = 0
_MAX_BIT_INDEX = 62
_MAX_PROGRESS_SET_COUNT = 100_000


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


@dataclass(frozen=True, slots=True)
class Curriculum:
    """A validated set of skills and their required preparation."""

    skills: tuple[Skill, ...]
    requirements: tuple[tuple[SkillCode, tuple[SkillCode, ...]], ...]
    prerequisite_sets: tuple[tuple[SkillCode, tuple[SkillCode, ...]], ...]
    reachable_masks: tuple[int, ...]
    version: str

    @classmethod
    def load(
        cls,
        skills: Iterable[Skill],
        requirements: Iterable[tuple[object, Iterable[object]]],
    ) -> "Curriculum":
        """Validate authored skills and requirements into an immutable curriculum."""
        loaded_skills = tuple(skills)
        skills_by_code = _skills_by_code(loaded_skills)
        ordered_skills = tuple(sorted(loaded_skills, key=lambda skill: skill.bit_index))
        normalized_requirements = _normalize_requirements(requirements, skills_by_code)
        direct_requirements = {
            code: normalized_requirements.get(code, ()) for code in skills_by_code
        }
        prerequisite_sets = _build_prerequisite_sets(
            direct_requirements, skills_by_code
        )
        ordered_requirements = tuple(
            (skill.code, direct_requirements[skill.code]) for skill in ordered_skills
        )
        return cls(
            skills=ordered_skills,
            requirements=ordered_requirements,
            prerequisite_sets=tuple(
                (skill.code, prerequisite_sets[skill.code]) for skill in ordered_skills
            ),
            reachable_masks=_enumerate_reachable_masks(
                ordered_skills, prerequisite_sets
            ),
            version=_version_for(ordered_skills, ordered_requirements),
        )

    def prerequisites_for(self, code: SkillCode) -> tuple[SkillCode, ...]:
        """Return every skill that must be completed before this one."""
        for current_code, prerequisite_set in self.prerequisite_sets:
            if current_code == code:
                return prerequisite_set
        raise UnknownSkill(f"skill {code!r} is not in this curriculum")

    def is_reachable(self, mask: int) -> bool:
        """Return whether a mask represents valid learner progress."""
        return type(mask) is int and mask in self.reachable_masks

    def next_up(self, mask: int) -> tuple[Skill, ...]:
        """Return skills a learner can start from their current progress."""
        self._require_reachable(mask)
        return tuple(
            skill
            for skill in self.skills
            if not mask & _skill_mask(skill)
            and self.is_reachable(mask | _skill_mask(skill))
        )

    def review_set(self, mask: int) -> tuple[Skill, ...]:
        """Return mastered skills that can be reviewed independently."""
        self._require_reachable(mask)
        return tuple(
            skill
            for skill in self.skills
            if mask & _skill_mask(skill)
            and self.is_reachable(mask & ~_skill_mask(skill))
        )

    def path_to(self, mask: int, code: SkillCode) -> tuple[Skill, ...]:
        """Return the ordered preparation needed to reach one skill."""
        self._require_reachable(mask)
        required = {code, *self.prerequisites_for(code)}
        available_mask = mask
        path: list[Skill] = []
        while required:
            ready = tuple(
                skill
                for skill in self.skills
                if skill.code in required
                and self.is_reachable(available_mask | _skill_mask(skill))
            )
            if not ready:
                raise InvalidProgress("the requested skill cannot be reached")
            for skill in ready:
                required.remove(skill.code)
                if not available_mask & _skill_mask(skill):
                    available_mask |= _skill_mask(skill)
                    path.append(skill)
        return tuple(path)

    def _require_reachable(self, mask: int) -> None:
        if not self.is_reachable(mask):
            raise InvalidProgress("progress mask is not valid for this curriculum")


def _skills_by_code(skills: tuple[Skill, ...]) -> dict[SkillCode, Skill]:
    skills_by_code: dict[SkillCode, Skill] = {}
    bit_indexes: set[int] = set()
    for skill in skills:
        if skill.code in skills_by_code:
            raise DuplicateSkill(f"skill {skill.code!r} is declared more than once")
        if skill.bit_index in bit_indexes:
            raise DuplicateSkill(f"bit index {skill.bit_index} is used more than once")
        skills_by_code[skill.code] = skill
        bit_indexes.add(skill.bit_index)
    if not skills_by_code:
        raise InvalidSkill("a curriculum must contain at least one skill")
    return skills_by_code


def _normalize_requirements(
    requirements: Iterable[tuple[object, Iterable[object]]],
    skills_by_code: dict[SkillCode, Skill],
) -> dict[SkillCode, tuple[SkillCode, ...]]:
    normalized: dict[SkillCode, tuple[SkillCode, ...]] = {}
    for declared_code, declared_requirements in requirements:
        code = _known_code(declared_code, skills_by_code)
        if code in normalized:
            raise DuplicateRequirement(f"requirements for {code!r} are repeated")
        seen: set[SkillCode] = set()
        prepared: list[SkillCode] = []
        for declared_requirement in declared_requirements:
            requirement = _known_code(declared_requirement, skills_by_code)
            if requirement == code:
                raise SelfRequirement(f"skill {code!r} cannot require itself")
            if requirement in seen:
                raise DuplicateRequirement(
                    f"skill {code!r} repeats requirement {requirement!r}"
                )
            seen.add(requirement)
            prepared.append(requirement)
        normalized[code] = tuple(prepared)
    return normalized


def _known_code(value: object, skills_by_code: dict[SkillCode, Skill]) -> SkillCode:
    try:
        code = skill_code(value)
    except InvalidIdentifier as error:
        raise UnknownSkill(f"invalid skill reference {value!r}") from error
    if code not in skills_by_code:
        raise UnknownSkill(f"skill {code!r} is not in this curriculum")
    return code


def _build_prerequisite_sets(
    requirements: dict[SkillCode, tuple[SkillCode, ...]],
    skills_by_code: dict[SkillCode, Skill],
) -> dict[SkillCode, tuple[SkillCode, ...]]:
    resolved: dict[SkillCode, tuple[SkillCode, ...]] = {}
    visiting: list[SkillCode] = []

    def resolve(code: SkillCode) -> tuple[SkillCode, ...]:
        if code in resolved:
            return resolved[code]
        if code in visiting:
            start = visiting.index(code)
            cycle = (*visiting[start:], code)
            raise PrerequisiteCycle(" -> ".join(cycle))
        visiting.append(code)
        found: set[SkillCode] = set()
        for requirement in requirements[code]:
            found.add(requirement)
            found.update(resolve(requirement))
        visiting.pop()
        ordered = tuple(sorted(found, key=lambda item: skills_by_code[item].bit_index))
        resolved[code] = ordered
        return ordered

    for skill in sorted(skills_by_code.values(), key=lambda item: item.bit_index):
        resolve(skill.code)
    return resolved


def _enumerate_reachable_masks(
    skills: tuple[Skill, ...], prerequisite_sets: dict[SkillCode, tuple[SkillCode, ...]]
) -> tuple[int, ...]:
    bit_indexes = {skill.code: skill.bit_index for skill in skills}
    prerequisites_by_code = {
        code: sum(1 << bit_indexes[requirement] for requirement in requirements)
        for code, requirements in prerequisite_sets.items()
    }
    masks = {0}
    queue = [0]
    cursor = 0
    while cursor < len(queue):
        mask = queue[cursor]
        cursor += 1
        for skill in skills:
            skill_mask = _skill_mask(skill)
            if mask & skill_mask:
                continue
            prerequisite_mask = prerequisites_by_code[skill.code]
            if prerequisite_mask & mask != prerequisite_mask:
                continue
            candidate = mask | skill_mask
            if candidate in masks:
                continue
            if len(masks) >= _MAX_PROGRESS_SET_COUNT:
                raise CurriculumTooLarge(
                    "curriculum exceeds the 100000 reachable-progress limit"
                )
            masks.add(candidate)
            queue.append(candidate)
    return tuple(sorted(masks))


def _version_for(
    skills: tuple[Skill, ...],
    requirements: tuple[tuple[SkillCode, tuple[SkillCode, ...]], ...],
) -> str:
    canonical = (
        tuple(
            (skill.code, skill.bit_index, skill.title, skill.card) for skill in skills
        ),
        tuple((code, tuple(required)) for code, required in requirements),
    )
    return sha256(repr(canonical).encode("utf-8")).hexdigest()


def _skill_mask(skill: Skill) -> int:
    return 1 << skill.bit_index
