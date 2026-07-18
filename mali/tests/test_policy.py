from dataclasses import replace
from fractions import Fraction

import pytest

from mali.policy import POLICY_V1, InvalidPolicy
from mali.templates import AnswerType


def test_default_policy_is_complete_and_uses_exact_rates() -> None:
    assert POLICY_V1.miss_rate(AnswerType.INTEGER) == Fraction(1, 10)
    assert POLICY_V1.lucky_rate(AnswerType.CHOICE) == Fraction(1, 4)


def test_policy_refuses_non_discriminating_rates() -> None:
    rates = tuple(
        (answer_type, Fraction(9, 10) if answer_type is AnswerType.INTEGER else rate)
        for answer_type, rate in POLICY_V1.lucky_rates
    )
    with pytest.raises(InvalidPolicy):
        replace(POLICY_V1, lucky_rates=rates)
