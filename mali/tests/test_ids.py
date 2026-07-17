from collections.abc import Callable

import pytest

from mali.errors import InvalidIdentifier
from mali.ids import checkpoint_id, learner_id, question_id, skill_code


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (learner_id, "learner_01"),
        (checkpoint_id, "check-point-01"),
        (question_id, "question01"),
        (skill_code, "add-fractions"),
    ],
)
def test_identifier_factories_accept_valid_values(
    factory: Callable[[object], str], value: str
) -> None:
    assert factory(value) == value


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (learner_id, ""),
        (checkpoint_id, "contains space"),
        (question_id, object()),
        (skill_code, "Uppercase"),
        (skill_code, "two_words"),
    ],
)
def test_identifier_factories_reject_invalid_values(
    factory: Callable[[object], str], value: object
) -> None:
    with pytest.raises(InvalidIdentifier):
        factory(value)
