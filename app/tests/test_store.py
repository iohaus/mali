from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
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
from mali.ids import LearnerId, learner_id, skill_code
from mali.policy import POLICY_V2
from mali.snapshot import Snapshot
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate

from mali_app.fresh import CountingFreshSource, SystemFreshSource
from mali_app.schema import open_database
from mali_app.store import (
    CheckInProgressError,
    CurriculumNotChosen,
    LearnerAlreadyRegistered,
    LearnerNotFound,
    SQLiteRecordStore,
    TopicNotAdopted,
)
from mali_app.store_types import ExecutionStatus


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _curriculum() -> Curriculum:
    template = QuestionTemplate(
        (ParameterDomain("number", tuple(range(8))),),
        "number",
        "What is {number}?",
        AnswerType.INTEGER,
    )
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand equal parts.", template)
    return Curriculum.load((parts,), ())


def _store(path: str) -> SQLiteRecordStore:
    return SQLiteRecordStore(path, clock=FrozenClock(), fresh=CountingFreshSource())


def _register_with_curriculum(
    store: SQLiteRecordStore, learner: LearnerId, display_name: str
) -> Snapshot:
    store.register(learner, display_name)
    return store.adopt_curriculum(
        learner,
        _curriculum(),
        title="Parts practice",
        summary="Understand equal parts one step at a time.",
    )


def _require_snapshot(
    result_status: ExecutionStatus, snapshot: Snapshot | None
) -> Snapshot:
    assert result_status is ExecutionStatus.COMMITTED
    assert snapshot is not None
    return snapshot


def _answer_open_question(
    store: SQLiteRecordStore, learner: LearnerId, *, correctly: bool = True
) -> Snapshot:
    before = store.snapshot(learner)
    assert before.checkpoint is not None
    skill = (
        before.checkpoint.target
        if before.checkpoint.target is not None
        else skill_code("parts")
    )
    asked = store.execute(
        learner,
        AskQuestion(skill, len(before.checkpoint.questions)),
        Actor.ENGINE,
    )
    after_question = _require_snapshot(asked.status, asked.snapshot)
    assert after_question.checkpoint is not None
    question = next(
        question
        for question in after_question.checkpoint.questions
        if question.answer is None
    )
    answer = question.instance.key if correctly else "999"
    answered = store.execute(
        learner, RecordAnswer(question.identifier, answer), Actor.STUDENT
    )
    return _require_snapshot(answered.status, answered.snapshot)


def _run_engine(store: SQLiteRecordStore, learner: LearnerId) -> Snapshot:
    snapshot = store.snapshot(learner)
    while True:
        action = TutorDesk.available(snapshot).engine_action
        if action is None:
            return snapshot
        result = store.execute(learner, action, Actor.ENGINE)
        snapshot = _require_snapshot(result.status, result.snapshot)


def test_record_store_commits_a_complete_deterministic_learning_flow(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "mali.db")
    store = _store(database)
    learner = learner_id("flow-learner")
    registered = _register_with_curriculum(store, learner, "Ada")
    assert not registered.progress.placed
    assert registered.policy.instructor_prompt_version == "instructor_v1"
    assert registered.policy.item_writer_prompt_version == "item_writer_v1"

    started = store.execute(learner, StartPlacement(), Actor.ENGINE)
    _require_snapshot(started.status, started.snapshot)
    for _ in range(POLICY_V2.question_budget):
        _answer_open_question(store, learner, correctly=False)
    placed = _run_engine(store, learner)
    assert placed.progress.placed
    assert placed.progress.mask == 0

    target = store.execute(learner, ProposeTarget(skill_code("parts")), Actor.STUDENT)
    _require_snapshot(target.status, target.snapshot)
    started_check = store.execute(learner, StartCheck(), Actor.ENGINE)
    _require_snapshot(started_check.status, started_check.snapshot)
    for _ in range(POLICY_V2.pass_rule.needed):
        _answer_open_question(store, learner)
    completed = _run_engine(store, learner)

    assert completed.progress.mask == 1
    assert completed.progress.target is None
    assert completed.checkpoint is None
    assert store.audit(learner).valid


def _second_curriculum() -> Curriculum:
    template = QuestionTemplate(
        (ParameterDomain("count", tuple(range(1, 9))),),
        "count * 2",
        "How many shoes fit {count} pairs?",
        AnswerType.INTEGER,
    )
    pairs = Skill(skill_code("pairs"), 0, "Pairs", "Count objects in pairs.", template)
    return Curriculum.load((pairs,), ())


def test_learner_topics_lists_only_that_learner_and_switching_preserves_progress(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "topics.db"))
    learner = learner_id("topic-learner")
    other = learner_id("other-learner")
    _register_with_curriculum(store, learner, "Ada")
    store.register(other, "Grace")
    first_version = store.snapshot(learner).progress.curriculum_version
    store.adopt_curriculum(
        learner,
        _second_curriculum(),
        title="Counting pairs",
        summary="Count in twos with everyday objects.",
    )

    person = store.learner_topics(learner)
    assert person.learner == learner
    assert person.display_name == "Ada"
    titles = {topic.title: topic for topic in person.topics}
    assert set(titles) == {"Parts practice", "Counting pairs"}
    assert titles["Counting pairs"].active
    assert not titles["Parts practice"].active
    assert titles["Parts practice"].skill_count == 1
    assert store.learner_topics(other).topics == ()

    resumed = store.switch_topic(learner, first_version)

    assert resumed.progress.curriculum_version == first_version
    refreshed = {topic.title: topic for topic in store.learner_topics(learner).topics}
    assert refreshed["Parts practice"].active
    assert not refreshed["Counting pairs"].active
    with pytest.raises(TopicNotAdopted):
        store.switch_topic(learner, "not-a-saved-version")
    with pytest.raises(LearnerNotFound):
        store.learner_topics(learner_id("never-registered"))


def test_registering_a_known_learner_is_a_typed_returning_state(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "returning.db"))
    learner = learner_id("returning-learner")
    store.register(learner, "Ada")

    with pytest.raises(LearnerAlreadyRegistered):
        store.register(learner, "Ada Again")

    audit = store.audit(learner)
    assert audit.valid


def test_learner_without_a_curriculum_is_a_typed_state_not_an_error(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "unstarted.db"))
    learner = learner_id("unstarted-learner")
    store.register(learner, "Ada")

    with pytest.raises(CurriculumNotChosen):
        store.snapshot(learner)
    audit = store.audit(learner)
    assert audit.valid
    assert "no curriculum chosen yet" in audit.detail


def test_adoption_is_refused_while_a_check_is_open(tmp_path: Path) -> None:
    store = _store(str(tmp_path / "open-check.db"))
    learner = learner_id("busy-learner")
    _register_with_curriculum(store, learner, "Ada")
    started = store.execute(learner, StartPlacement(), Actor.ENGINE)
    _require_snapshot(started.status, started.snapshot)

    with pytest.raises(CheckInProgressError):
        store.adopt_curriculum(
            learner,
            _second_curriculum(),
            title="Pairs practice",
            summary="Count in twos with confidence.",
        )


def test_placement_certifies_skills_the_learner_demonstrates(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "ace.db"))
    learner = learner_id("ace-learner")
    _register_with_curriculum(store, learner, "Ada")

    started = store.execute(learner, StartPlacement(), Actor.ENGINE)
    _require_snapshot(started.status, started.snapshot)
    for _ in range(POLICY_V2.question_budget):
        _answer_open_question(store, learner)
    placed = _run_engine(store, learner)

    assert placed.progress.placed
    assert placed.progress.mask == 1
    assert store.audit(learner).valid


def test_switching_curricula_keeps_each_record_separate_and_audit_clean(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "switch.db"))
    learner = learner_id("switch-learner")
    _register_with_curriculum(store, learner, "Ada")

    started = store.execute(learner, StartPlacement(), Actor.ENGINE)
    _require_snapshot(started.status, started.snapshot)
    for _ in range(POLICY_V2.question_budget):
        _answer_open_question(store, learner)
    placed = _run_engine(store, learner)
    assert placed.progress.placed
    first_version = placed.progress.curriculum_version

    switched = store.adopt_curriculum(
        learner,
        _second_curriculum(),
        title="Pairs practice",
        summary="Count in twos with confidence.",
    )
    assert switched.progress.curriculum_version != first_version
    assert not switched.progress.placed
    assert switched.progress.mask == 0
    assert store.audit(learner).valid

    returned = store.adopt_curriculum(
        learner,
        _curriculum(),
        title="Parts practice",
        summary="Understand equal parts one step at a time.",
    )
    assert returned.progress.curriculum_version == first_version
    assert returned.progress.placed
    assert store.audit(learner).valid


def test_conflicting_expected_versions_commit_once_and_report_a_stale_record(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "conflict.db")
    learner = learner_id("conflict-learner")
    initial = SQLiteRecordStore(database, clock=FrozenClock())
    _register_with_curriculum(initial, learner, "Ada")
    connection = open_database(database)
    try:
        connection.execute(
            "UPDATE progress SET placed = 1, version = 1 WHERE learner = ?", (learner,)
        )
    finally:
        connection.close()

    barrier = Barrier(2)

    def fire() -> ExecutionStatus:
        store = SQLiteRecordStore(
            database,
            clock=FrozenClock(),
            fresh=SystemFreshSource(),
        )
        barrier.wait()
        return store.execute(
            learner,
            ProposeTarget(skill_code("parts")),
            Actor.STUDENT,
            expected_version=1,
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fire)
        second = pool.submit(fire)
        results = (first.result(), second.result())

    assert set(results) == {ExecutionStatus.COMMITTED, ExecutionStatus.STALE_RECORD}
    connection = open_database(database)
    try:
        entries = connection.execute(
            "SELECT COUNT(*) FROM learning_journal WHERE learner = ?", (learner,)
        ).fetchone()
        assert entries is not None
        assert entries[0] == 1
    finally:
        connection.close()


def test_assumed_skills_are_saved_with_the_adopted_curriculum(
    tmp_path: Path,
) -> None:
    store = _store(str(tmp_path / "assumed.db"))
    learner = learner_id("assumed-learner")
    store.register(learner, "Ada")
    adopted = store.adopt_curriculum(
        learner,
        _curriculum(),
        title="Parts practice",
        summary="Understand equal parts one step at a time.",
        assumed=("parts",),
    )

    version = adopted.progress.curriculum_version
    assert store.assumed_skill_codes(version) == frozenset({"parts"})
    assert store.assumed_skill_codes("unknown-version") == frozenset()
