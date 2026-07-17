import pytest

from mali.curriculum import Curriculum, Skill
from mali.errors import (
    CurriculumTooLarge,
    DuplicateRequirement,
    DuplicateSkill,
    PrerequisiteCycle,
    SelfRequirement,
    UnknownSkill,
)
from mali.ids import skill_code


def _skill(code: str, bit_index: int) -> Skill:
    return Skill(
        code=skill_code(code),
        bit_index=bit_index,
        title=code.replace("-", " ").title(),
        card="Practice this skill with focused examples.",
    )


def test_curriculum_resolves_transitive_requirements_in_bit_order() -> None:
    curriculum = Curriculum.load(
        (_skill("compare", 2), _skill("add", 1), _skill("parts", 0)),
        (("add", ("parts",)), ("compare", ("add",))),
    )

    assert tuple(skill.code for skill in curriculum.skills) == (
        skill_code("parts"),
        skill_code("add"),
        skill_code("compare"),
    )
    assert curriculum.prerequisites_for(skill_code("compare")) == (
        skill_code("parts"),
        skill_code("add"),
    )


@pytest.mark.parametrize(
    ("skills", "requirements", "error"),
    [
        ((_skill("parts", 0), _skill("parts", 1)), (), DuplicateSkill),
        ((_skill("parts", 0), _skill("add", 0)), (), DuplicateSkill),
        ((_skill("parts", 0),), (("missing", ()),), UnknownSkill),
        ((_skill("parts", 0),), (("parts", ("missing",)),), UnknownSkill),
        ((_skill("parts", 0),), (("parts", ("parts",)),), SelfRequirement),
        (
            (_skill("parts", 0), _skill("add", 1)),
            (("add", ("parts", "parts")),),
            DuplicateRequirement,
        ),
        (
            (_skill("parts", 0),),
            (("parts", ()), ("parts", ())),
            DuplicateRequirement,
        ),
    ],
)
def test_curriculum_rejects_invalid_authored_requirements(
    skills: tuple[Skill, ...],
    requirements: tuple[tuple[str, tuple[str, ...]], ...],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        Curriculum.load(skills, requirements)


def test_curriculum_reports_the_cycle_path() -> None:
    with pytest.raises(PrerequisiteCycle, match="parts -> add -> compare -> parts"):
        Curriculum.load(
            (_skill("parts", 0), _skill("add", 1), _skill("compare", 2)),
            (("parts", ("add",)), ("add", ("compare",)), ("compare", ("parts",))),
        )


def test_curriculum_enumerates_progress_and_answers_queries() -> None:
    parts = _skill("parts", 0)
    add = _skill("add", 1)
    compare = _skill("compare", 2)
    curriculum = Curriculum.load(
        (parts, add, compare), (("add", ("parts",)), ("compare", ("add",)))
    )

    assert curriculum.reachable_masks == (0, 1, 3, 7)
    assert curriculum.is_reachable(3)
    assert not curriculum.is_reachable(5)
    assert curriculum.next_up(1) == (add,)
    assert curriculum.review_set(3) == (add,)
    assert curriculum.path_to(0, skill_code("compare")) == (parts, add, compare)


def test_curriculum_version_is_deterministic() -> None:
    skills = (_skill("parts", 0), _skill("add", 1))
    requirements = (("add", ("parts",)),)

    assert (
        Curriculum.load(skills, requirements).version
        == Curriculum.load(tuple(reversed(skills)), requirements).version
    )


def test_curriculum_refuses_an_explosive_progress_set() -> None:
    skills = tuple(_skill(f"skill-{index}", index) for index in range(17))

    with pytest.raises(CurriculumTooLarge):
        Curriculum.load(skills, ())
