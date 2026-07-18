from mali.actions import Actor, OverrideMastery, ProposeTarget, TeachEpisode
from mali.curriculum import Curriculum, Skill
from mali.ids import learner_id, skill_code
from mali.progress import Progress
from mali.rules import (
    Allowed,
    RefusalReason,
    Refused,
    override_mastery,
    propose_target,
    teach_episode,
)


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
