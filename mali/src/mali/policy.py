"""Versioned tutoring policy values validated before use."""

from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction

from mali.errors import MaliDomainError
from mali.templates import AnswerType


class InvalidPolicy(MaliDomainError):
    """Raised when tutoring policy values are not safe to apply."""


@dataclass(frozen=True, slots=True)
class PassRule:
    """The number of correct answers required from a bounded check."""

    needed: int
    asked: int

    def __post_init__(self) -> None:
        if type(self.needed) is not int or type(self.asked) is not int:
            raise InvalidPolicy("pass rule values must be integers")
        if not 1 <= self.needed <= self.asked:
            raise InvalidPolicy("pass rule must require between one and all questions")


@dataclass(frozen=True, slots=True)
class FlowBudget:
    """Bounded model-flow resources for one tutoring episode."""

    max_turns: int
    max_requests: int
    max_output_tokens: int
    max_episode_tokens: int
    item_writer_retries: int

    def __post_init__(self) -> None:
        values = (
            self.max_turns,
            self.max_requests,
            self.max_output_tokens,
            self.max_episode_tokens,
            self.item_writer_retries,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise InvalidPolicy("flow budgets must be positive integers")


@dataclass(frozen=True, slots=True)
class TutorPolicy:
    """A complete, immutable set of tutoring decision parameters."""

    version: str
    certify_threshold: Fraction
    question_budget: int
    pass_rule: PassRule
    miss_rates: tuple[tuple[AnswerType, Fraction], ...]
    lucky_rates: tuple[tuple[AnswerType, Fraction], ...]
    stale_after: timedelta
    flow_budget: FlowBudget

    def __post_init__(self) -> None:
        if not self.version:
            raise InvalidPolicy("policy version must not be blank")
        if not Fraction(1, 2) < self.certify_threshold <= 1:
            raise InvalidPolicy("certification threshold must be above one half")
        if type(self.question_budget) is not int or self.question_budget < 1:
            raise InvalidPolicy("question budget must be positive")
        if self.pass_rule.asked > self.question_budget:
            raise InvalidPolicy("pass rule cannot exceed the question budget")
        if self.stale_after <= timedelta(0):
            raise InvalidPolicy("checkpoint lifetime must be positive")
        miss = dict(self.miss_rates)
        lucky = dict(self.lucky_rates)
        expected = set(AnswerType)
        if set(miss) != expected or set(lucky) != expected:
            raise InvalidPolicy("every answer type needs miss and lucky rates")
        for answer_type in AnswerType:
            miss_rate, lucky_rate = miss[answer_type], lucky[answer_type]
            if not 0 < miss_rate < 1 or lucky_rate < 0 or lucky_rate >= 1 - miss_rate:
                raise InvalidPolicy(
                    "response rates must discriminate skill from guessing"
                )

    def miss_rate(self, answer_type: AnswerType) -> Fraction:
        """Return the configured miss rate for one answer type."""
        return dict(self.miss_rates)[answer_type]

    def lucky_rate(self, answer_type: AnswerType) -> Fraction:
        """Return the configured lucky-answer rate for one answer type."""
        return dict(self.lucky_rates)[answer_type]


_DEFAULT_MISS_RATES = tuple(
    (answer_type, Fraction(1, 10)) for answer_type in AnswerType
)
_DEFAULT_LUCKY_RATES = (
    (AnswerType.INTEGER, Fraction(0)),
    (AnswerType.FRACTION, Fraction(0)),
    (AnswerType.EXACT, Fraction(0)),
    (AnswerType.CHOICE, Fraction(1, 4)),
)
POLICY_V1 = TutorPolicy(
    version="v1",
    certify_threshold=Fraction(4, 5),
    question_budget=5,
    pass_rule=PassRule(needed=3, asked=4),
    miss_rates=_DEFAULT_MISS_RATES,
    lucky_rates=_DEFAULT_LUCKY_RATES,
    stale_after=timedelta(hours=24),
    flow_budget=FlowBudget(8, 3, 800, 4_000, 2),
)
