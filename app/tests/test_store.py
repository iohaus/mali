from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

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
from mali.policy import POLICY_V1
from mali.snapshot import Snapshot
from mali.templates import AnswerType, ParameterDomain, QuestionTemplate

from mali_app.fresh import CountingFreshSource, SystemFreshSource
from mali_app.schema import open_database
from mali_app.store import SQLiteRecordStore
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
    return SQLiteRecordStore(
        path,
        _curriculum(),
        clock=FrozenClock(),
        fresh=CountingFreshSource(),
    )


def _require_snapshot(
    result_status: ExecutionStatus, snapshot: Snapshot | None
) -> Snapshot:
    assert result_status is ExecutionStatus.COMMITTED
    assert snapshot is not None
    return snapshot


def _answer_open_question(store: SQLiteRecordStore, learner: LearnerId) -> Snapshot:
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
    answered = store.execute(
        learner, RecordAnswer(question.identifier, question.instance.key), Actor.STUDENT
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
    registered = store.register(learner, "Ada")
    assert not registered.progress.placed

    started = store.execute(learner, StartPlacement(), Actor.ENGINE)
    _require_snapshot(started.status, started.snapshot)
    for _ in range(POLICY_V1.question_budget):
        _answer_open_question(store, learner)
    placed = _run_engine(store, learner)
    assert placed.progress.placed

    target = store.execute(learner, ProposeTarget(skill_code("parts")), Actor.STUDENT)
    _require_snapshot(target.status, target.snapshot)
    started_check = store.execute(learner, StartCheck(), Actor.ENGINE)
    _require_snapshot(started_check.status, started_check.snapshot)
    for _ in range(POLICY_V1.pass_rule.needed):
        _answer_open_question(store, learner)
    completed = _run_engine(store, learner)

    assert completed.progress.mask == 1
    assert completed.progress.target is None
    assert completed.checkpoint is None
    assert store.audit(learner).valid


def test_conflicting_expected_versions_commit_once_and_report_a_stale_record(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "conflict.db")
    learner = learner_id("conflict-learner")
    curriculum = _curriculum()
    initial = SQLiteRecordStore(database, curriculum, clock=FrozenClock())
    initial.register(learner, "Ada")
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
            curriculum,
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
