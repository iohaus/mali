from hypothesis import given
from hypothesis import strategies as st

from mali.curriculum import Curriculum, Skill
from mali.ids import skill_code


@st.composite
def _authored_curricula(
    draw: st.DrawFn,
) -> tuple[tuple[Skill, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    skill_count = draw(st.integers(min_value=1, max_value=8))
    skills = tuple(
        Skill(
            code=skill_code(f"skill-{index}"),
            bit_index=index,
            title=f"Skill {index}",
            card=f"Practice skill {index}.",
        )
        for index in range(skill_count)
    )
    requirements: list[tuple[str, tuple[str, ...]]] = []
    for index in range(skill_count):
        required_indexes = draw(
            st.lists(
                st.integers(min_value=0, max_value=index - 1),
                unique=True,
                max_size=index,
            )
            if index
            else st.just([])
        )
        if required_indexes:
            requirements.append(
                (
                    f"skill-{index}",
                    tuple(
                        f"skill-{required_index}" for required_index in required_indexes
                    ),
                )
            )
    return skills, tuple(requirements)


def _brute_force_masks(
    skills: tuple[Skill, ...], requirements: tuple[tuple[str, tuple[str, ...]], ...]
) -> tuple[int, ...]:
    requirement_map = dict(requirements)
    masks: list[int] = []
    for mask in range(1 << len(skills)):
        valid = all(
            not mask & (1 << skill.bit_index)
            or all(
                mask & (1 << int(required.rsplit("-", maxsplit=1)[1]))
                for required in requirement_map.get(skill.code, ())
            )
            for skill in skills
        )
        if valid:
            masks.append(mask)
    return tuple(masks)


@given(_authored_curricula())
def test_enumeration_matches_brute_force(
    authored: tuple[tuple[Skill, ...], tuple[tuple[str, tuple[str, ...]], ...]],
) -> None:
    skills, requirements = authored
    curriculum = Curriculum.load(skills, requirements)

    assert curriculum.reachable_masks == _brute_force_masks(skills, requirements)


@given(_authored_curricula())
def test_reachable_progress_is_closed_under_union_and_intersection(
    authored: tuple[tuple[Skill, ...], tuple[tuple[str, tuple[str, ...]], ...]],
) -> None:
    skills, requirements = authored
    curriculum = Curriculum.load(skills, requirements)
    reachable_masks = set(curriculum.reachable_masks)

    for left in curriculum.reachable_masks:
        for right in curriculum.reachable_masks:
            assert left | right in reachable_masks
            assert left & right in reachable_masks


@given(_authored_curricula())
def test_readiness_and_review_queries_are_available_when_needed(
    authored: tuple[tuple[Skill, ...], tuple[tuple[str, tuple[str, ...]], ...]],
) -> None:
    skills, requirements = authored
    curriculum = Curriculum.load(skills, requirements)
    full_mask = (1 << len(skills)) - 1

    for mask in curriculum.reachable_masks:
        if mask != full_mask:
            assert curriculum.next_up(mask)
        if mask:
            assert curriculum.review_set(mask)
