"""HTTP adapter for the deterministic local Mali tutoring flow."""

from fastapi import FastAPI, HTTPException, status
from mali.actions import (
    Action,
    Actor,
    AskQuestion,
    ProposeTarget,
    RecordAnswer,
    StartCheck,
    StartPlacement,
)
from mali.checkpoint import Question
from mali.desk import TutorDesk
from mali.errors import InvalidIdentifier
from mali.ids import LearnerId, learner_id, question_id, skill_code
from mali.rules import RefusalReason
from mali.snapshot import Snapshot
from mali.views import progress_map
from pydantic import BaseModel

from mali_app.demo import demo_curriculum
from mali_app.schema import DatabasePath
from mali_app.store import LearnerNotFound, SQLiteRecordStore, StoreError
from mali_app.store_types import ExecutionResult, ExecutionStatus

_DEFAULT_DATABASE = "mali.db"
_REFUSAL_COPY = {
    RefusalReason.NOT_READY_YET: "That skill comes later. Start with the next step.",
    RefusalReason.ALREADY_MASTERED: "You have already completed that skill.",
    RefusalReason.CHECK_IN_PROGRESS: "Finish the current check before changing course.",
    RefusalReason.PLACEMENT_ALREADY_DONE: "Your starting check is already complete.",
    RefusalReason.PLACEMENT_REQUIRED: (
        "Start with a short check so we can pick a good next step."
    ),
    RefusalReason.NOTHING_TO_CHECK: "There is no active check right now.",
    RefusalReason.NOT_CURRENT_TARGET: "That is not the skill currently being checked.",
    RefusalReason.TEACHER_REQUIRED: "A teacher needs to make that change.",
    RefusalReason.INVALID_ACTOR: "That request is not available here.",
    RefusalReason.QUESTION_NOT_FOUND: "That question is not part of the current check.",
    RefusalReason.QUESTION_ALREADY_ANSWERED: "That question already has an answer.",
    RefusalReason.ANSWER_NOT_READABLE: "Please enter an answer in the requested form.",
    RefusalReason.PLACEMENT_NOT_READY: "Answer the remaining questions first.",
    RefusalReason.CHECK_NOT_DECIDED: (
        "Answer another question before finishing the check."
    ),
}


class RegisterRequest(BaseModel):
    """The public fields required to create a local learner record."""

    learner_id: str
    display_name: str


class AnswerRequest(BaseModel):
    """A learner's raw response to one displayed question."""

    question_id: str
    answer: str


def create_app(database: DatabasePath = _DEFAULT_DATABASE) -> FastAPI:
    """Create the small FastAPI product surface backed by one SQLite file."""
    store = SQLiteRecordStore(database, demo_curriculum())
    app = FastAPI(title="Mali", version="v1")

    def register(request: RegisterRequest) -> dict[str, object]:
        learner = _learner_or_error(request.learner_id)
        try:
            snapshot = store.register(learner, request.display_name)
        except StoreError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT, "learner_exists", str(error)
            ) from error
        return _progress_response(snapshot)

    def get_progress(learner: str) -> dict[str, object]:
        snapshot = _snapshot_or_error(store, learner)
        return _progress_response(snapshot)

    def start_placement(learner: str) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        _snapshot_or_error(store, learner)
        result = _execute_or_error(store, identifier, StartPlacement(), Actor.ENGINE)
        return _progress_response(_committed_snapshot(result))

    def propose_target(learner: str, skill: str) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        _snapshot_or_error(store, learner)
        try:
            action = ProposeTarget(skill_code(skill))
        except InvalidIdentifier as error:
            raise _request_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_skill", str(error)
            ) from error
        result = _execute_or_error(store, identifier, action, Actor.STUDENT)
        return _progress_response(_committed_snapshot(result))

    def start_check(learner: str) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        _snapshot_or_error(store, learner)
        result = _execute_or_error(store, identifier, StartCheck(), Actor.ENGINE)
        return _progress_response(_committed_snapshot(result))

    def current_question(learner: str) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        snapshot = _snapshot_or_error(store, learner)
        snapshot = _ensure_question(store, identifier, snapshot)
        checkpoint = snapshot.checkpoint
        if checkpoint is None:
            raise _request_error(
                status.HTTP_409_CONFLICT,
                "no_question",
                "There is no question to answer.",
            )
        question = next(
            (question for question in checkpoint.questions if question.answer is None),
            None,
        )
        if question is None:
            raise _request_error(
                status.HTTP_409_CONFLICT,
                "no_question",
                "There is no question to answer.",
            )
        return _question_response(question)

    def submit_answer(learner: str, request: AnswerRequest) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        _snapshot_or_error(store, learner)
        try:
            action = RecordAnswer(question_id(request.question_id), request.answer)
        except InvalidIdentifier as error:
            raise _request_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_question", str(error)
            ) from error
        result = _execute_or_error(store, identifier, action, Actor.STUDENT)
        snapshot = _drain_engine(store, identifier, _committed_snapshot(result))
        return {"accepted": True, "progress": _progress_response(snapshot)}

    app.add_api_route(
        "/v1/learners",
        register,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
    )
    app.add_api_route("/v1/learners/{learner}/progress", get_progress, methods=["GET"])
    app.add_api_route(
        "/v1/learners/{learner}/placement", start_placement, methods=["POST"]
    )
    app.add_api_route(
        "/v1/learners/{learner}/targets/{skill}", propose_target, methods=["POST"]
    )
    app.add_api_route("/v1/learners/{learner}/check", start_check, methods=["POST"])
    app.add_api_route(
        "/v1/learners/{learner}/question", current_question, methods=["GET"]
    )
    app.add_api_route("/v1/learners/{learner}/answers", submit_answer, methods=["POST"])
    return app


def _snapshot_or_error(store: SQLiteRecordStore, learner: str) -> Snapshot:
    identifier = _learner_or_error(learner)
    try:
        return store.snapshot(identifier)
    except LearnerNotFound as error:
        raise _request_error(
            status.HTTP_404_NOT_FOUND, "learner_not_found", "Learner not found."
        ) from error
    except StoreError as error:
        raise _request_error(
            status.HTTP_409_CONFLICT, "record_unavailable", str(error)
        ) from error


def _execute_or_error(
    store: SQLiteRecordStore,
    learner: LearnerId,
    action: Action,
    actor: Actor,
) -> ExecutionResult:
    try:
        result = store.execute(learner, action, actor)
    except (LearnerNotFound, StoreError) as error:
        raise _request_error(
            status.HTTP_409_CONFLICT, "record_unavailable", str(error)
        ) from error
    if result.status is ExecutionStatus.REFUSED and result.refusal is not None:
        raise _request_error(
            status.HTTP_409_CONFLICT,
            result.refusal.value,
            _REFUSAL_COPY[result.refusal],
        )
    if result.status is ExecutionStatus.STALE_RECORD:
        raise _request_error(
            status.HTTP_409_CONFLICT,
            "stale_record",
            "Your record changed. Please try again.",
        )
    return result


def _ensure_question(
    store: SQLiteRecordStore, learner: LearnerId, snapshot: Snapshot
) -> Snapshot:
    checkpoint = snapshot.checkpoint
    if checkpoint is None or any(
        question.answer is None for question in checkpoint.questions
    ):
        return snapshot
    if len(checkpoint.questions) >= snapshot.policy.question_budget:
        return snapshot
    skill = checkpoint.target
    if skill is None:
        ready = snapshot.progress.curriculum.next_up(snapshot.progress.mask)
        if not ready:
            return snapshot
        skill = ready[0].code
    result = _execute_or_error(
        store, learner, AskQuestion(skill, len(checkpoint.questions)), Actor.ENGINE
    )
    return _committed_snapshot(result)


def _drain_engine(
    store: SQLiteRecordStore, learner: LearnerId, snapshot: Snapshot
) -> Snapshot:
    while True:
        action = TutorDesk.available(snapshot).engine_action
        if action is None:
            return snapshot
        result = store.execute(learner, action, Actor.ENGINE)
        if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
            raise _request_error(
                status.HTTP_409_CONFLICT,
                "record_changed",
                "Your record changed. Please try again.",
            )
        snapshot = result.snapshot


def _committed_snapshot(result: ExecutionResult) -> Snapshot:
    if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
        raise AssertionError("successful HTTP action must commit")
    return result.snapshot


def _learner_or_error(value: str) -> LearnerId:
    try:
        return learner_id(value)
    except InvalidIdentifier as error:
        raise _request_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_learner", str(error)
        ) from error


def _progress_response(snapshot: Snapshot) -> dict[str, object]:
    mapped = progress_map(snapshot.progress, snapshot.progress.curriculum)
    return {
        "learner_id": snapshot.progress.learner,
        "placed": snapshot.progress.placed,
        "current_skill": snapshot.progress.target,
        "mastered": mapped.mastered,
        "next_up": mapped.next_up,
        "later": mapped.later,
    }


def _question_response(question: Question) -> dict[str, object]:
    return {
        "question_id": question.identifier,
        "prompt": question.instance.text,
        "answer_type": question.instance.answer_type.value,
        "options": question.instance.options,
    }


def _request_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )
