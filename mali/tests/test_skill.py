import pytest

from mali.curriculum import Skill
from mali.errors import InvalidSkill
from mali.ids import skill_code


def test_skill_accepts_valid_assessable_content() -> None:
    skill = Skill(
        code=skill_code("add-fractions"),
        bit_index=0,
        title="Add fractions with common denominators",
        card="Add the numerators and keep the denominator.",
    )

    assert skill.bit_index == 0


@pytest.mark.parametrize(
    ("bit_index", "title", "card"),
    [
        (-1, "Valid title", "Valid card"),
        (63, "Valid title", "Valid card"),
        (True, "Valid title", "Valid card"),
        (0, "", "Valid card"),
        (0, "Valid title", "   "),
    ],
)
def test_skill_rejects_malformed_content(bit_index: int, title: str, card: str) -> None:
    with pytest.raises(InvalidSkill):
        Skill(
            code=skill_code("add-fractions"),
            bit_index=bit_index,
            title=title,
            card=card,
        )
