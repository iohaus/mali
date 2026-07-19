import pytest

from mali.errors import InvalidTemplate
from mali.templates import (
    AnswerType,
    Constraint,
    ConstraintKind,
    DisplayValue,
    ParameterDomain,
    QuestionTemplate,
    canonical_answer,
    validate_rendering,
)


def _addition_template() -> QuestionTemplate:
    return QuestionTemplate(
        parameters=(
            ParameterDomain("left", (1, 2, 3, 4)),
            ParameterDomain("right", (1, 2, 3, 4)),
        ),
        key_expression="left + right",
        plain_template="What is {left} + {right}?",
        answer_type=AnswerType.INTEGER,
        constraints=(Constraint(ConstraintKind.DISTINCT, ("left", "right")),),
    )


def test_template_selects_a_deterministic_verified_instance() -> None:
    template = _addition_template()

    first = template.instance(42)

    assert first == template.instance(42)
    assert first.key in {"3", "4", "5", "6", "7"}
    assert validate_rendering(first, first.text).accepted


def test_template_supports_derived_display_values_and_choices() -> None:
    template = QuestionTemplate(
        parameters=(ParameterDomain("number", tuple(range(8))),),
        key_expression="number",
        plain_template="Choose {number}; its double is {double}: {options}?",
        answer_type=AnswerType.CHOICE,
        display_values=(DisplayValue("double", "number * 2"),),
        options=tuple(str(number) for number in range(8)),
    )

    instance = template.instance(3)

    assert instance.key in instance.options
    assert validate_rendering(instance, instance.text).accepted


@pytest.mark.parametrize(
    ("answer_type", "raw", "expected"),
    [
        (AnswerType.INTEGER, " 03 ", "3"),
        (AnswerType.INTEGER, "3/2", None),
        (AnswerType.FRACTION, "0.5", "1/2"),
        (AnswerType.EXACT, "6/4", "3/2"),
        (AnswerType.CHOICE, "Option A", "Option A"),
    ],
)
def test_canonical_answer(
    answer_type: AnswerType, raw: str, expected: str | None
) -> None:
    assert canonical_answer(answer_type, raw) == expected


def test_template_refuses_invalid_variants_before_use() -> None:
    with pytest.raises(InvalidTemplate, match="divides by zero"):
        QuestionTemplate(
            parameters=(ParameterDomain("divisor", (0, 1, 2, 3, 4, 5, 6, 7)),),
            key_expression="1 / divisor",
            plain_template="What is 1 divided by {divisor}?",
            answer_type=AnswerType.FRACTION,
        )


def test_template_supports_remainder_and_whole_number_division() -> None:
    template = QuestionTemplate(
        parameters=(ParameterDomain("hour", tuple(range(13, 24))),),
        key_expression="hour % 12",
        plain_template="A clock read out loud: what hour name matches {hour}:00?",
        answer_type=AnswerType.INTEGER,
    )
    grouped = QuestionTemplate(
        parameters=(ParameterDomain("eggs", tuple(range(13, 25))),),
        key_expression="eggs // 12",
        plain_template="How many full cartons of 12 can you fill with {eggs} eggs?",
        answer_type=AnswerType.INTEGER,
    )

    hour_instance = template.instance(7)
    eggs_instance = grouped.instance(9)

    hour = next(value for name, value in hour_instance.values if name == "hour")
    eggs = next(value for name, value in eggs_instance.values if name == "eggs")
    assert hour_instance.key == str(hour % 12)
    assert eggs_instance.key == str(eggs // 12)


def test_template_names_the_unsupported_operator() -> None:
    with pytest.raises(InvalidTemplate, match="Pow"):
        QuestionTemplate(
            parameters=(
                ParameterDomain(
                    "side",
                    tuple(range(2, 10)),
                ),
            ),
            key_expression="side ** 2",
            plain_template="What is the area of a square with side {side}?",
            answer_type=AnswerType.INTEGER,
        )


def test_rendering_rejects_missing_values_and_revealed_answers() -> None:
    instance = _addition_template().instance(5)

    assert not validate_rendering(instance, "What is 1 plus 2?").accepted
    assert not validate_rendering(instance, f"What is {instance.key} + 2?").accepted
