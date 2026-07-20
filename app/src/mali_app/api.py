"""HTTP adapter for the deterministic local Mali tutoring flow."""

import logging
from collections.abc import Iterator
from json import dumps

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from mali.actions import (
    Action,
    Actor,
    AskQuestion,
    ProposeTarget,
    RecordAnswer,
    StartCheck,
    StartPlacement,
    TeachEpisode,
)
from mali.checkpoint import Question
from mali.desk import TutorDesk
from mali.errors import InvalidIdentifier
from mali.ids import LearnerId, learner_id, question_id, skill_code
from mali.rules import RefusalReason, Refused, evaluate
from mali.snapshot import Snapshot
from mali.views import progress_map
from pydantic import BaseModel

from mali_app.curriculum_author import CurriculumAuthor, CurriculumBuildError
from mali_app.degradation import DegradationController, DegradationLevel
from mali_app.instructor import InstructorEpisode, InstructorEvent
from mali_app.item_writer import ItemWriter
from mali_app.model_gateway import ModelGateway
from mali_app.model_providers import create_model_gateway_from_environment
from mali_app.schema import DatabasePath
from mali_app.store import (
    CheckInProgressError,
    CurriculumNotChosen,
    LearnerNotFound,
    SQLiteRecordStore,
    StoreError,
)
from mali_app.store_types import ExecutionResult, ExecutionStatus
from mali_app.web import install_web_routes, placement_probe

_DEFAULT_DATABASE = "mali.db"
_LOG = logging.getLogger(__name__)
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


class LessonRequest(BaseModel):
    """One student turn supplied when opening or continuing a lesson."""

    student_turn: str = ""


class CurriculumRequest(BaseModel):
    """A learner's description of what they would like to learn."""

    topic: str


def create_app(
    database: DatabasePath = _DEFAULT_DATABASE,
    *,
    enable_item_writer: bool = False,
    enable_instructor: bool = False,
    model_gateway: ModelGateway | None = None,
    degradation: DegradationController | None = None,
) -> FastAPI:
    """Create the small FastAPI product surface backed by one SQLite file."""
    store = SQLiteRecordStore(database)
    controller = (
        DegradationController.from_environment() if degradation is None else degradation
    )
    needs_gateway = (
        enable_instructor and controller.level is not DegradationLevel.STATIC
    ) or (enable_item_writer and controller.level is DegradationLevel.NORMAL)
    gateway = (
        model_gateway
        if model_gateway is not None
        else create_model_gateway_from_environment()
        if needs_gateway
        else None
    )
    item_writer = (
        ItemWriter(gateway) if enable_item_writer and gateway is not None else None
    )
    instructor = (
        InstructorEpisode(store, gateway, controller) if enable_instructor else None
    )
    curriculum_author = CurriculumAuthor(gateway)
    _LOG.info(
        "application configured database=%s instructor=%s item_writer=%s "
        "curriculum_author=%s level=%s",
        database,
        enable_instructor,
        enable_item_writer,
        gateway is not None,
        controller.level.value,
    )
    app = FastAPI(title="Mali", version="v1")

    def register(request: RegisterRequest) -> dict[str, object]:
        learner = _learner_or_error(request.learner_id)
        try:
            store.register(learner, request.display_name)
        except StoreError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT, "learner_exists", str(error)
            ) from error
        return _unstarted_response(learner)

    def get_progress(learner: str) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        try:
            snapshot = store.snapshot(identifier)
        except CurriculumNotChosen:
            return _unstarted_response(identifier)
        except LearnerNotFound as error:
            raise _request_error(
                status.HTTP_404_NOT_FOUND, "learner_not_found", "Learner not found."
            ) from error
        except StoreError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT, "record_unavailable", str(error)
            ) from error
        return _progress_response(snapshot)

    def build_curriculum(learner: str, request: CurriculumRequest) -> dict[str, object]:
        identifier = _learner_or_error(learner)
        try:
            authored = curriculum_author.build(identifier, request.topic)
            snapshot = store.adopt_curriculum(
                identifier,
                authored.curriculum,
                title=authored.title,
                summary=authored.summary,
            )
        except CurriculumBuildError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT, "curriculum_unavailable", str(error)
            ) from error
        except LearnerNotFound as error:
            raise _request_error(
                status.HTTP_404_NOT_FOUND, "learner_not_found", "Learner not found."
            ) from error
        except CheckInProgressError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT,
                "check_in_progress",
                "Finish the current check before changing course.",
            ) from error
        except StoreError as error:
            raise _request_error(
                status.HTTP_409_CONFLICT, "record_unavailable", str(error)
            ) from error
        return {
            "title": authored.title,
            "summary": authored.summary,
            "skills": [skill.title for skill in snapshot.progress.curriculum.skills],
        }

    def start_placement(learner: str) -> dict[str, object]:
        _LOG.info("start placement learner=%s", learner)
        identifier = _learner_or_error(learner)
        _snapshot_or_error(store, learner)
        result = _execute_or_error(store, identifier, StartPlacement(), Actor.ENGINE)
        progress = _progress_response(_committed_snapshot(result))
        _LOG.debug("placement started learner=%s progress=%s", learner, progress
        )
        return progress

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
        prompt = (
            question.instance.text
            if item_writer is None
            else _item_writer_prompt(snapshot, question, item_writer, controller)
        )
        return _question_response(question, prompt)

    def lesson(learner: str, request: LessonRequest) -> StreamingResponse:
        identifier = _learner_or_error(learner)
        snapshot = _snapshot_or_error(store, learner)
        target = snapshot.progress.target
        if instructor is None:
            raise _request_error(
                status.HTTP_404_NOT_FOUND,
                "lesson_unavailable",
                "Lessons are not enabled for this service.",
            )
        if target is None:
            raise _request_error(
                status.HTTP_409_CONFLICT,
                "no_active_lesson",
                "Choose a skill before starting a lesson.",
            )
        verdict = evaluate(
            TeachEpisode(target),
            snapshot.progress,
            snapshot.checkpoint,
            Actor.INSTRUCTOR,
            snapshot.policy,
        )
        if isinstance(verdict, Refused):
            refusal = verdict.reason
            raise _request_error(
                status.HTTP_409_CONFLICT, refusal.value, _REFUSAL_COPY[refusal]
            )
        return StreamingResponse(
            _sse_stream(instructor.stream(identifier, snapshot, request.student_turn)),
            media_type="text/event-stream",
        )

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
        "/v1/learners/{learner}/curriculum",
        build_curriculum,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
    )
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
    app.add_api_route("/v1/learners/{learner}/lesson", lesson, methods=["POST"])
    app.add_api_route("/v1/learners/{learner}/answers", submit_answer, methods=["POST"])
    web_instructor = (
        instructor
        if instructor is not None
        else InstructorEpisode(store, None, controller)
    )

    def web_question_prompt(snapshot: Snapshot, question: Question) -> str:
        if item_writer is None:
            return question.instance.text
        return _item_writer_prompt(snapshot, question, item_writer, controller)

    install_web_routes(
        app, store, web_instructor, web_question_prompt, curriculum_author
    )
    return app


def _snapshot_or_error(store: SQLiteRecordStore, learner: str) -> Snapshot:
    identifier = _learner_or_error(learner)
    try:
        return store.snapshot(identifier)
    except LearnerNotFound as error:
        raise _request_error(
            status.HTTP_404_NOT_FOUND, "learner_not_found", "Learner not found."
        ) from error
    except CurriculumNotChosen as error:
        raise _request_error(
            status.HTTP_409_CONFLICT,
            "curriculum_required",
            "Choose what you would like to learn first.",
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
    _LOG.debug("execute action=%s learner=%s actor=%s", action, learner, actor)
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
        skill = placement_probe(snapshot, checkpoint)
        if skill is None:
            return snapshot
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


def _unstarted_response(learner: LearnerId) -> dict[str, object]:
    """Describe a learner who has not yet chosen what to learn."""
    return {
        "learner_id": learner,
        "placed": False,
        "current_skill": None,
        "mastered": (),
        "next_up": (),
        "later": (),
    }


def _item_writer_prompt(
    snapshot: Snapshot,
    question: Question,
    item_writer: ItemWriter,
    degradation: DegradationController,
) -> str:
    skill = next(
        item
        for item in snapshot.progress.curriculum.skills
        if item.code == question.skill
    )
    if skill.template is None:
        return question.instance.text
    checkpoint = snapshot.checkpoint
    checkpoint_id = checkpoint.identifier if checkpoint is not None else None
    if not degradation.use_item_writer(checkpoint_id):
        return question.instance.text
    result = item_writer.render(snapshot.policy, skill.template, question.instance)
    degradation.report_item_writer(
        checkpoint_id,
        used_fallback=result.used_fallback,
        gateway_failed=result.gateway_failed,
    )
    if result.used_fallback:
        _LOG.warning(
            "question rendering used deterministic fallback "
            "checkpoint=%s gateway_failed=%s",
            checkpoint_id,
            result.gateway_failed,
        )
    return result.question_text


def _sse_stream(events: Iterator[InstructorEvent]) -> Iterator[str]:
    """Encode typed Instructor events as a minimal server-sent-event stream."""
    for event in events:
        payload: dict[str, str] = {}
        if event.text is not None:
            payload["text"] = event.text
        if event.outcome is not None:
            payload["outcome"] = event.outcome.value
        yield f"data: {dumps(payload, separators=(',', ':'))}\n\n"


def _question_response(
    question: Question, prompt: str | None = None
) -> dict[str, object]:
    return {
        "question_id": question.identifier,
        "prompt": question.instance.text if prompt is None else prompt,
        "answer_type": question.instance.answer_type.value,
        "options": question.instance.options,
    }


def _request_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )
