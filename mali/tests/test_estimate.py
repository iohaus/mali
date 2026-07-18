from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from mali.checkpoint import Question
from mali.curriculum import Curriculum, Skill
from mali.estimate import PlacementEstimate
from mali.ids import question_id, skill_code
from mali.policy import POLICY_V1
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate


def _curriculum() -> Curriculum:
    return Curriculum.load(
        (
            Skill(skill_code("one"), 0, "One", "One."),
            Skill(skill_code("two"), 1, "Two", "Two."),
        ),
        (),
    )


def _question(code: str) -> Question:
    instance = QuestionTemplate(
        (ParameterDomain("number", tuple(range(8))),),
        "number",
        "What is {number}?",
        AnswerType.INTEGER,
    ).instance(1)
    return Question(question_id("question-1"), skill_code(code), instance)


def test_estimate_updates_exactly_and_selects_deterministically() -> None:
    curriculum = _curriculum()
    estimate = PlacementEstimate.uniform(curriculum)
    updated = estimate.updated(_question("one"), True, curriculum, POLICY_V1)

    assert sum(weight for _, weight in updated.weights) == 1
    assert updated.pick_question(curriculum, POLICY_V1) == "two"
    assert updated.certified_mask() in curriculum.reachable_masks
    assert all(isinstance(weight, Fraction) for _, weight in updated.weights)


def test_no_informative_question_means_one_mask_has_all_weight() -> None:
    curriculum = _curriculum()
    estimate = PlacementEstimate(((3, Fraction(1)),))

    assert estimate.pick_question(curriculum, POLICY_V1) is None
    assert estimate.certified_mask() == 3


@given(st.lists(st.booleans(), min_size=1, max_size=12))
def test_estimate_stays_normalized_and_non_negative(verdicts: list[bool]) -> None:
    curriculum = _curriculum()
    estimate = PlacementEstimate.uniform(curriculum)
    question = _question("one")

    for verdict in verdicts:
        estimate = estimate.updated(question, verdict, curriculum, POLICY_V1)
        assert sum(weight for _, weight in estimate.weights) == 1
        assert all(weight >= 0 for _, weight in estimate.weights)
        assert estimate.pick_question(curriculum, POLICY_V1) == estimate.pick_question(
            curriculum, POLICY_V1
        )


@given(st.lists(st.booleans(), max_size=12))
def test_placement_run_is_bounded_by_the_policy_budget(verdicts: list[bool]) -> None:
    curriculum = _curriculum()
    estimate = PlacementEstimate.uniform(curriculum)
    question = _question("one")
    steps = 0

    while (
        estimate.pick_question(curriculum, POLICY_V1) is not None
        and max(weight for _, weight in estimate.weights) < POLICY_V1.certify_threshold
        and steps < POLICY_V1.question_budget
    ):
        verdict = verdicts[steps] if steps < len(verdicts) else False
        estimate = estimate.updated(question, verdict, curriculum, POLICY_V1)
        steps += 1

    assert steps <= POLICY_V1.question_budget
