from mali.actions import Actor, OverrideMastery, ProposeTarget, TeachEpisode
from mali.checkpoint import Answer, CheckPoint, CheckPointKind, Question
from mali.curriculum import Curriculum, Skill
from mali.ids import checkpoint_id, learner_id, question_id, skill_code
from mali.policy import POLICY_V1
from mali.progress import Progress
from mali.rules import (
    Allowed,
    RefusalReason,
    Refused,
    next_engine_action,
    override_mastery,
    propose_target,
    teach_episode,
)
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate


def _curriculum() -> Curriculum:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand equal parts.")
    add = Skill(skill_code("add"), 1, "Add", "Add equal parts.")
    return Curriculum.load((parts, add), (("add", ("parts",)),))


def _progress(placed: bool = True) -> Progress:
    curriculum = _curriculum()
    return Progress(
        learner_id("learner-rules"), curriculum.version, 0, placed, None, 0, curriculum
    )


def test_ready_target_is_allowed_for_student() -> None:
    assert isinstance(
        propose_target(
            _progress(), None, ProposeTarget(skill_code("parts")), Actor.STUDENT
        ),
        Allowed,
    )


def test_teacher_override_requires_teacher_actor() -> None:
    verdict = override_mastery(
        _progress(),
        None,
        OverrideMastery(skill_code("parts"), "observed"),
        Actor.STUDENT,
    )
    assert isinstance(verdict, Refused)
    assert verdict.reason is RefusalReason.TEACHER_REQUIRED


def test_instructor_cannot_teach_a_non_target() -> None:
    verdict = teach_episode(
        _progress(), TeachEpisode(skill_code("parts")), Actor.INSTRUCTOR
    )
    assert isinstance(verdict, Refused)
    assert verdict.reason is RefusalReason.NOT_CURRENT_TARGET


def test_engine_prioritizes_a_decided_mastery_check() -> None:
    instance = QuestionTemplate(
        (ParameterDomain("number", tuple(range(8))),),
        "number",
        "What is {number}?",
        AnswerType.INTEGER,
    ).instance(1)
    questions = tuple(
        Question(
            question_id(f"rule-question-{index}"),
            skill_code("parts"),
            instance,
            Answer(instance.key, True),
        )
        for index in range(POLICY_V1.pass_rule.needed)
    )
    checkpoint = CheckPoint(
        checkpoint_id("rule-check"),
        CheckPointKind.CHECK,
        skill_code("parts"),
        questions,
    )
    assert next_engine_action(checkpoint, POLICY_V1).__class__.__name__ == "PassCheck"
