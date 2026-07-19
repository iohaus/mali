"""Small deterministic curriculum and record history for the local demo."""

from mali.actions import (
    Actor,
    AskQuestion,
    ProposeTarget,
    RecordAnswer,
    StartCheck,
    StartPlacement,
)
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.ids import learner_id, skill_code
from mali.policy import POLICY_V1
from mali.snapshot import Snapshot
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate

from mali_app.store import (
    CurriculumNotChosen,
    LearnerNotFound,
    SQLiteRecordStore,
    StoreError,
)
from mali_app.store_types import ExecutionResult, ExecutionStatus

DEMO_LEARNER = learner_id("demo-learner")
DEMO_LEARNER_NAME = "Mali demo learner"
DEMO_CURRICULUM_TITLE = "Fraction foundations"
DEMO_CURRICULUM_SUMMARY = (
    "Build confidence with halves and quarters, one checked step at a time."
)
_DEMO_SKILL = skill_code("equal-halves")


def demo_curriculum() -> Curriculum:
    """Create the small fraction curriculum used by local demo commands."""
    halves_template = QuestionTemplate(
        (ParameterDomain("numerator", tuple(range(8))),),
        "(numerator + 1) / 2",
        "What is {numerator}/2 + 1/2?",
        AnswerType.FRACTION,
    )
    quarters_template = QuestionTemplate(
        (ParameterDomain("numerator", tuple(range(8))),),
        "(numerator + 1) / 4",
        "What is {numerator}/4 + 1/4?",
        AnswerType.FRACTION,
    )
    equal_halves = Skill(
        _DEMO_SKILL,
        0,
        "Equal halves",
        "A whole can be split into two equal halves.",
        halves_template,
    )
    adding_halves = Skill(
        skill_code("adding-halves"),
        1,
        "Adding halves",
        "When the parts match, add the top numbers and keep the same halves.",
        halves_template,
    )
    adding_quarters = Skill(
        skill_code("adding-quarters"),
        2,
        "Adding quarters",
        "Four equal parts make a whole, so matching quarters can be combined.",
        quarters_template,
    )
    return Curriculum.load(
        (equal_halves, adding_halves, adding_quarters),
        (
            (adding_halves.code, (equal_halves.code,)),
            (adding_quarters.code, (adding_halves.code,)),
        ),
    )


def seed_demo(store: SQLiteRecordStore) -> Snapshot:
    """Create and complete the compact history shown in the local demo."""
    try:
        snapshot = store.snapshot(DEMO_LEARNER)
    except LearnerNotFound:
        store.register(DEMO_LEARNER, DEMO_LEARNER_NAME)
        snapshot = _adopt_demo_curriculum(store)
    except CurriculumNotChosen:
        snapshot = _adopt_demo_curriculum(store)
    if not snapshot.progress.placed:
        _committed(store.execute(DEMO_LEARNER, StartPlacement(), Actor.ENGINE))
        _complete_open_checkpoint(store)
        snapshot = _run_engine(store)
    if snapshot.progress.mask == 0:
        _committed(
            store.execute(
                DEMO_LEARNER,
                ProposeTarget(_DEMO_SKILL),
                Actor.STUDENT,
            )
        )
        _committed(store.execute(DEMO_LEARNER, StartCheck(), Actor.ENGINE))
        _complete_open_checkpoint(store)
        snapshot = _run_engine(store)
    return snapshot


def _adopt_demo_curriculum(store: SQLiteRecordStore) -> Snapshot:
    return store.adopt_curriculum(
        DEMO_LEARNER,
        demo_curriculum(),
        title=DEMO_CURRICULUM_TITLE,
        summary=DEMO_CURRICULUM_SUMMARY,
    )


def _complete_open_checkpoint(store: SQLiteRecordStore) -> None:
    while True:
        snapshot = store.snapshot(DEMO_LEARNER)
        checkpoint = snapshot.checkpoint
        if checkpoint is None:
            return
        if len(checkpoint.questions) >= POLICY_V1.question_budget:
            return
        skill = checkpoint.target if checkpoint.target is not None else _DEMO_SKILL
        asked = _committed(
            store.execute(
                DEMO_LEARNER,
                AskQuestion(skill, len(checkpoint.questions)),
                Actor.ENGINE,
            )
        )
        if asked.checkpoint is None:
            raise StoreError("question action unexpectedly closed the checkpoint")
        question = next(
            question
            for question in asked.checkpoint.questions
            if question.answer is None
        )
        _committed(
            store.execute(
                DEMO_LEARNER,
                RecordAnswer(question.identifier, question.instance.key),
                Actor.STUDENT,
            )
        )
        if TutorDesk.available(store.snapshot(DEMO_LEARNER)).engine_action is not None:
            return


def _run_engine(store: SQLiteRecordStore) -> Snapshot:
    snapshot = store.snapshot(DEMO_LEARNER)
    while True:
        action = TutorDesk.available(snapshot).engine_action
        if action is None:
            return snapshot
        snapshot = _committed(store.execute(DEMO_LEARNER, action, Actor.ENGINE))


def _committed(result: ExecutionResult) -> Snapshot:
    if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
        raise StoreError(f"demo action was not committed: {result.status}")
    return result.snapshot
