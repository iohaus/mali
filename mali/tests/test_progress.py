import pytest

from mali.checkpoint import (
    Answer,
    CheckPoint,
    CheckPointKind,
    Question,
    cannot_pass,
    has_passed,
)
from mali.curriculum import Curriculum, Skill
from mali.errors import InvalidCheckpoint, InvalidProgress
from mali.ids import checkpoint_id, learner_id, question_id, skill_code
from mali.progress import Progress
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate


def _curriculum() -> Curriculum:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand equal parts.")
    add = Skill(skill_code("add"), 1, "Add", "Add equal parts.")
    return Curriculum.load((parts, add), (("add", ("parts",)),))


def _instance():
    return QuestionTemplate(
        parameters=(ParameterDomain("value", tuple(range(8))),),
        key_expression="value",
        plain_template="What is {value}?",
        answer_type=AnswerType.INTEGER,
    ).instance(2)


def test_progress_requires_a_ready_target_after_placement() -> None:
    curriculum = _curriculum()
    progress = Progress(
        learner=learner_id("learner-1"),
        curriculum_version=curriculum.version,
        mask=0,
        placed=True,
        target=skill_code("parts"),
        version=0,
        curriculum=curriculum,
    )

    assert progress.target == skill_code("parts")


def test_progress_refuses_a_target_without_requirements() -> None:
    curriculum = _curriculum()

    with pytest.raises(InvalidProgress):
        Progress(
            learner=learner_id("learner-1"),
            curriculum_version=curriculum.version,
            mask=0,
            placed=True,
            target=skill_code("add"),
            version=0,
            curriculum=curriculum,
        )


def test_question_recomputes_the_answer_verdict() -> None:
    instance = _instance()
    answer = Answer(value=instance.key, correct=True)

    question = Question(
        question_id("question-1"), skill_code("parts"), instance, answer
    )

    assert question.answer == answer


def test_question_refuses_a_forged_verdict() -> None:
    instance = _instance()

    with pytest.raises(InvalidCheckpoint):
        Question(
            question_id("question-1"),
            skill_code("parts"),
            instance,
            Answer(value=instance.key, correct=False),
        )


def test_check_point_and_pass_arithmetic() -> None:
    instance = _instance()
    correct = Question(
        question_id("question-1"),
        skill_code("parts"),
        instance,
        Answer(instance.key, True),
    )
    incorrect = Question(
        question_id("question-2"), skill_code("parts"), instance, Answer("99", False)
    )
    checkpoint = CheckPoint(
        checkpoint_id("check-1"),
        CheckPointKind.CHECK,
        skill_code("parts"),
        (correct, incorrect),
    )

    assert has_passed(checkpoint.questions, 1)
    assert cannot_pass(checkpoint.questions, 2, 2)
