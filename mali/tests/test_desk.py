from mali.actions import Actor, AskQuestion, ProposeTarget, RecordAnswer, SkipPlacement
from mali.checkpoint import CheckPoint, CheckPointKind
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.ids import checkpoint_id, learner_id, skill_code
from mali.plans import CheckPointWrite, ProgressWrite
from mali.policy import POLICY_V2
from mali.progress import Progress
from mali.rules import RefusalReason, Refused
from mali.snapshot import Snapshot
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate


def _snapshot(placed: bool = True) -> Snapshot:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand parts.")
    add = Skill(skill_code("add"), 1, "Add", "Add parts.")
    curriculum = Curriculum.load((parts, add), (("add", ("parts",)),))
    progress = Progress(
        learner_id("desk-learner"), curriculum.version, 0, placed, None, 0, curriculum
    )
    return Snapshot(progress, None, POLICY_V2, checkpoint_id("desk-check"))


def test_plan_rechecks_rules_before_producing_writes() -> None:
    snapshot = _snapshot()
    plan = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)

    assert not isinstance(plan, Refused)
    assert isinstance(plan.writes[0], ProgressWrite)
    assert plan.writes[0].progress.target == skill_code("parts")


def test_refused_actions_do_not_produce_a_plan() -> None:
    plan = TutorDesk.plan(ProposeTarget(skill_code("add")), _snapshot(), Actor.STUDENT)

    assert isinstance(plan, Refused)
    assert plan.reason is RefusalReason.NOT_READY_YET


def test_skipping_placement_resolves_a_start_without_crediting_skills() -> None:
    unplaced = _snapshot(placed=False)
    assert TutorDesk.available(unplaced).targets == ()

    plan = TutorDesk.plan(SkipPlacement(), unplaced, Actor.STUDENT)

    assert not isinstance(plan, Refused)
    write = plan.writes[0]
    assert isinstance(write, ProgressWrite)
    assert write.progress.placed
    assert write.progress.mask == 0
    assert write.progress.version == 1

    resolved = Snapshot(write.progress, None, POLICY_V2, None)
    assert TutorDesk.available(resolved).targets == (
        (skill_code("parts"), (skill_code("parts"),)),
    )
    assert not TutorDesk.available(resolved).can_start_placement


def test_available_lists_ready_targets_with_their_path() -> None:
    available = TutorDesk.available(_snapshot())

    assert available.targets == ((skill_code("parts"), (skill_code("parts"),)),)


def test_question_and_answer_plans_preserve_the_canonical_answer() -> None:
    template = QuestionTemplate(
        (ParameterDomain("number", tuple(range(8))),),
        "number",
        "What is {number}?",
        AnswerType.INTEGER,
    )
    skill = Skill(skill_code("parts"), 0, "Parts", "Understand parts.", template)
    curriculum = Curriculum.load((skill,), ())
    progress = Progress(
        learner_id("answer-learner"), curriculum.version, 0, False, None, 0, curriculum
    )
    checkpoint = CheckPoint(
        checkpoint_id("answer-check"), CheckPointKind.PLACEMENT, None, ()
    )
    snapshot = Snapshot(progress, checkpoint, POLICY_V2, None)
    asked = TutorDesk.plan(AskQuestion(skill.code, 7), snapshot, Actor.ENGINE)
    assert not isinstance(asked, Refused)
    write = asked.writes[0]
    assert isinstance(write, CheckPointWrite)
    assert write.checkpoint is not None
    question = write.checkpoint.questions[0]

    answered = TutorDesk.plan(
        RecordAnswer(question.identifier, f"  {question.instance.key}  "),
        Snapshot(progress, write.checkpoint, POLICY_V2, None),
        Actor.STUDENT,
    )
    assert not isinstance(answered, Refused)
    answer_write = answered.writes[0]
    assert isinstance(answer_write, CheckPointWrite)
    assert answer_write.checkpoint is not None
    assert answer_write.checkpoint.questions[0].answer is not None
    assert answer_write.checkpoint.questions[0].answer.correct
