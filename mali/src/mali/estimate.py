"""Exact placement estimates and deterministic question selection."""

from dataclasses import dataclass
from fractions import Fraction

from mali.checkpoint import Question
from mali.curriculum import Curriculum
from mali.errors import MaliDomainError
from mali.policy import TutorPolicy
from mali.templates import AnswerType


class InvalidEstimate(MaliDomainError):
    """Raised when placement weights are not a valid distribution."""


@dataclass(frozen=True, slots=True)
class PlacementEstimate:
    """An exact distribution over the valid progress masks of a curriculum."""

    weights: tuple[tuple[int, Fraction], ...]

    def __post_init__(self) -> None:
        if not self.weights or sum(weight for _, weight in self.weights) != 1:
            raise InvalidEstimate("placement weights must sum to one")
        if any(weight < 0 for _, weight in self.weights):
            raise InvalidEstimate("placement weights must not be negative")
        masks = tuple(mask for mask, _ in self.weights)
        if len(set(masks)) != len(masks):
            raise InvalidEstimate("placement masks must be unique")

    @classmethod
    def uniform(cls, curriculum: Curriculum) -> "PlacementEstimate":
        """Create the initial equal-weight placement estimate."""
        weight = Fraction(1, len(curriculum.reachable_masks))
        return cls(tuple((mask, weight) for mask in curriculum.reachable_masks))

    @classmethod
    def from_answers(
        cls,
        questions: tuple[Question, ...],
        curriculum: Curriculum,
        policy: TutorPolicy,
    ) -> "PlacementEstimate":
        """Fold every graded answer into one exact placement estimate."""
        estimate = cls.uniform(curriculum)
        for question in questions:
            if question.answer is None:
                continue
            estimate = estimate.updated(
                question, question.answer.correct, curriculum, policy
            )
        return estimate

    def updated(
        self,
        question: Question,
        verdict: bool,
        curriculum: Curriculum,
        policy: TutorPolicy,
    ) -> "PlacementEstimate":
        """Apply one exact answer update to the placement estimate."""
        bit = _bit_for(curriculum, question.skill)
        answer_type = question.instance.answer_type
        miss, lucky = policy.miss_rate(answer_type), policy.lucky_rate(answer_type)
        weighted = tuple(
            (mask, weight * _likelihood(verdict, bool(mask & bit), miss, lucky))
            for mask, weight in self.weights
        )
        total = sum(weight for _, weight in weighted)
        if total <= 0:
            raise InvalidEstimate("placement update has no possible result")
        return PlacementEstimate(
            tuple((mask, weight / total) for mask, weight in weighted)
        )

    def certified_mask(self) -> int:
        """Return the most-supported mask, breaking ties by lowest mask."""
        return min(self.weights, key=lambda item: (-item[1], item[0]))[0]

    def pick_question(self, curriculum: Curriculum, policy: TutorPolicy) -> str | None:
        """Choose the most informative remaining curriculum skill."""
        positive = tuple((mask, weight) for mask, weight in self.weights if weight > 0)
        candidates: list[tuple[Fraction, int, str]] = []
        for skill in curriculum.skills:
            bit = 1 << skill.bit_index
            if not any(mask & bit for mask, _ in positive) or all(
                mask & bit for mask, _ in positive
            ):
                continue
            miss, lucky = (
                policy.miss_rate(AnswerType.INTEGER),
                policy.lucky_rate(AnswerType.INTEGER),
            )
            chance = sum(
                weight * (1 - miss if mask & bit else lucky)
                for mask, weight in positive
            )
            candidates.append(
                (abs(chance - Fraction(1, 2)), skill.bit_index, skill.code)
            )
        return min(candidates)[2] if candidates else None


def _bit_for(curriculum: Curriculum, code: str) -> int:
    for skill in curriculum.skills:
        if skill.code == code:
            return 1 << skill.bit_index
    raise InvalidEstimate("question skill is not in the curriculum")


def _likelihood(
    verdict: bool, included: bool, miss: Fraction, lucky: Fraction
) -> Fraction:
    correct = 1 - miss if included else lucky
    return correct if verdict else 1 - correct
