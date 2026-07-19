"""Server-rendered student and teacher surfaces for the local product."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from json import dumps
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mali.actions import (
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
from mali.ids import (
    LearnerId,
    QuestionId,
    SkillCode,
    learner_id,
    question_id,
    skill_code,
)
from mali.rules import RefusalReason
from mali.snapshot import Snapshot
from mali.views import progress_map

from mali_app.instructor import InstructorEpisode, InstructorEvent
from mali_app.store import LearnerNotFound, SQLiteRecordStore, StoreError
from mali_app.store_types import ExecutionResult, ExecutionStatus

_ASSET_ROOT = Path(__file__).parent
_TEMPLATE_ROOT = _ASSET_ROOT / "templates"
_STATIC_ROOT = _ASSET_ROOT / "static"


@dataclass(frozen=True, slots=True)
class StudentQuestion:
    """One active question prepared for the inline student form."""

    identifier: QuestionId
    prompt: str
    answer_type: str
    options: tuple[str, ...]


def install_web_routes(
    app: FastAPI,
    store: SQLiteRecordStore,
    instructor: InstructorEpisode,
    question_prompt: Callable[[Snapshot, Question], str],
) -> None:
    """Attach Jinja, HTMX, and SSE routes to one configured application."""
    templates = Jinja2Templates(directory=str(_TEMPLATE_ROOT))
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")

    def home(request: Request) -> HTMLResponse:
        return _render(templates, request, "home.html", {})

    async def register_student(request: Request) -> Response:
        form = await request.form()
        raw_learner = _form_text(form.get("learner_id"))
        display_name = _form_text(form.get("display_name"))
        try:
            learner = learner_id(raw_learner)
            store.register(learner, display_name)
        except (InvalidIdentifier, StoreError):
            return _render(
                templates,
                request,
                "home.html",
                {"error": "Please choose a short learner ID and a display name."},
                status_code=400,
            )
        return RedirectResponse(f"/learners/{learner}", status_code=303)

    def student_page(request: Request, learner: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        try:
            context = _student_context(request, store, identifier, question_prompt)
        except (LearnerNotFound, StoreError):
            return _not_found(templates, request)
        return _render(templates, request, "student.html", context)

    def begin_placement(request: Request, learner: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        try:
            snapshot = store.snapshot(identifier)
            result = store.execute(identifier, StartPlacement(), Actor.ENGINE)
            if (
                result.status is ExecutionStatus.COMMITTED
                and result.snapshot is not None
            ):
                return _student_update(
                    templates,
                    request,
                    store,
                    identifier,
                    question_prompt,
                    snapshot=result.snapshot,
                    feedback="Here is your first question. Take your time.",
                )
            return _student_update(
                templates,
                request,
                store,
                identifier,
                question_prompt,
                snapshot=snapshot,
                feedback=_refusal_copy(result),
            )
        except (LearnerNotFound, StoreError):
            return _not_found(templates, request)

    def choose_skill(request: Request, learner: str, skill: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        try:
            requested = skill_code(skill)
            snapshot = store.snapshot(identifier)
            result = store.execute(
                identifier,
                ProposeTarget(requested),
                Actor.STUDENT,
                expected_version=snapshot.progress.version,
            )
        except (InvalidIdentifier, LearnerNotFound, StoreError):
            return _not_found(templates, request)
        if result.status is ExecutionStatus.COMMITTED and result.snapshot is not None:
            title = _skill_title(result.snapshot, requested)
            return _student_update(
                templates,
                request,
                store,
                identifier,
                question_prompt,
                snapshot=result.snapshot,
                feedback=f"Great choice. Let’s work on {title}.",
                lesson_url=_lesson_url(
                    identifier, f"I want to work on {title}.", requested
                ),
            )
        path = _path_titles(snapshot, requested)
        lesson_url = (
            _lesson_url(
                identifier,
                f"I want to work on {_skill_title(snapshot, requested)}.",
                requested,
            )
            if result.refusal is RefusalReason.NOT_READY_YET
            and snapshot.progress.target is not None
            else None
        )
        return _student_update(
            templates,
            request,
            store,
            identifier,
            question_prompt,
            snapshot=snapshot,
            feedback=_path_message(path),
            lesson_url=lesson_url,
        )

    def begin_check(request: Request, learner: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        try:
            snapshot = store.snapshot(identifier)
            result = store.execute(identifier, StartCheck(), Actor.ENGINE)
            if (
                result.status is ExecutionStatus.COMMITTED
                and result.snapshot is not None
            ):
                return _student_update(
                    templates,
                    request,
                    store,
                    identifier,
                    question_prompt,
                    snapshot=result.snapshot,
                    feedback="A short check will help you show what you understand.",
                )
            return _student_update(
                templates,
                request,
                store,
                identifier,
                question_prompt,
                snapshot=snapshot,
                feedback=_refusal_copy(result),
            )
        except (LearnerNotFound, StoreError):
            return _not_found(templates, request)

    async def submit_answer(request: Request, learner: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        form = await request.form()
        try:
            answer_identifier = question_id(_form_text(form.get("question_id")))
            raw_answer = _form_text(form.get("answer"))
            result = store.execute(
                identifier, RecordAnswer(answer_identifier, raw_answer), Actor.STUDENT
            )
        except (InvalidIdentifier, LearnerNotFound, StoreError):
            return _not_found(templates, request)
        if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
            return _student_update(
                templates,
                request,
                store,
                identifier,
                question_prompt,
                feedback=_refusal_copy(result),
            )
        correct = _answer_was_correct(result.snapshot, answer_identifier)
        snapshot = _drain_engine(store, identifier, result.snapshot)
        feedback = (
            "Nice work. Let’s build on that."
            if correct
            else "Not quite. Keep going—the next step will help."
        )
        return _student_update(
            templates,
            request,
            store,
            identifier,
            question_prompt,
            snapshot=snapshot,
            feedback=feedback,
        )

    def lesson_stream(
        learner: str,
        student_turn: str = "",
        requested_skill: str | None = None,
    ) -> StreamingResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return StreamingResponse(
                _static_events("That learner could not be found."),
                media_type="text/event-stream",
            )
        try:
            snapshot = store.snapshot(identifier)
        except (LearnerNotFound, StoreError):
            return StreamingResponse(
                _static_events("That learner could not be found."),
                media_type="text/event-stream",
            )
        path: tuple[str, ...] = ()
        if requested_skill is not None:
            try:
                requested = skill_code(requested_skill)
                path = tuple(
                    item.code
                    for item in snapshot.progress.curriculum.path_to(
                        snapshot.progress.mask, requested
                    )
                )
            except (InvalidIdentifier, ValueError):
                return StreamingResponse(
                    _static_events("Let’s choose one of the skills on your map."),
                    media_type="text/event-stream",
                )
        if snapshot.progress.target is None:
            return StreamingResponse(
                _static_events("Choose a next step and I’ll be ready to help."),
                media_type="text/event-stream",
            )
        return StreamingResponse(
            _sse_events(
                instructor.stream(
                    identifier,
                    snapshot,
                    student_turn,
                    prerequisite_path=path,
                )
            ),
            media_type="text/event-stream",
        )

    def teacher_dashboard(request: Request) -> HTMLResponse:
        try:
            learners = store.teacher_dashboard()
        except StoreError:
            return _render(
                templates,
                request,
                "teacher.html",
                {"learners": (), "error": "Teacher records are unavailable right now."},
                status_code=409,
            )
        return _render(templates, request, "teacher.html", {"learners": learners})

    def teacher_detail(request: Request, learner: str) -> HTMLResponse:
        identifier = _learner_or_none(learner)
        if identifier is None:
            return _not_found(templates, request)
        try:
            detail = store.teacher_detail(identifier)
        except (LearnerNotFound, StoreError):
            return _not_found(templates, request)
        return _render(templates, request, "teacher_detail.html", {"detail": detail})

    app.add_api_route("/", home, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/learners", register_student, methods=["POST"])
    app.add_api_route(
        "/learners/{learner}",
        student_page,
        methods=["GET"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        "/learners/{learner}/placement", begin_placement, methods=["POST"]
    )
    app.add_api_route(
        "/learners/{learner}/targets/{skill}", choose_skill, methods=["POST"]
    )
    app.add_api_route("/learners/{learner}/check", begin_check, methods=["POST"])
    app.add_api_route("/learners/{learner}/answers", submit_answer, methods=["POST"])
    app.add_api_route(
        "/learners/{learner}/lesson/stream", lesson_stream, methods=["GET"]
    )
    app.add_api_route("/teacher", teacher_dashboard, methods=["GET"])
    app.add_api_route("/teacher/{learner}", teacher_detail, methods=["GET"])


def _student_update(
    templates: Jinja2Templates,
    request: Request,
    store: SQLiteRecordStore,
    learner: LearnerId,
    question_prompt: Callable[[Snapshot, Question], str],
    *,
    snapshot: Snapshot | None = None,
    feedback: str | None = None,
    lesson_url: str | None = None,
) -> HTMLResponse:
    try:
        context = _student_context(
            request,
            store,
            learner,
            question_prompt,
            snapshot=snapshot,
            feedback=feedback,
            lesson_url=lesson_url,
        )
    except (LearnerNotFound, StoreError):
        return _not_found(templates, request)
    return _render(templates, request, "partials/student_content.html", context)


def _student_context(
    request: Request,
    store: SQLiteRecordStore,
    learner: LearnerId,
    question_prompt: Callable[[Snapshot, Question], str],
    *,
    snapshot: Snapshot | None = None,
    feedback: str | None = None,
    lesson_url: str | None = None,
) -> dict[str, object]:
    current = store.snapshot(learner) if snapshot is None else snapshot
    current, question = _active_question(store, learner, current, question_prompt)
    mapped = progress_map(current.progress, current.progress.curriculum)
    target = current.progress.target
    target_title = _skill_title(current, target) if target is not None else None
    return {
        "request": request,
        "learner": learner,
        "question": question,
        "progress": mapped,
        "placed": current.progress.placed,
        "target_title": target_title,
        "feedback": feedback,
        "lesson_url": lesson_url,
        "status_copy": _status_copy(current, question, target_title),
    }


def _active_question(
    store: SQLiteRecordStore,
    learner: LearnerId,
    snapshot: Snapshot,
    question_prompt: Callable[[Snapshot, Question], str],
) -> tuple[Snapshot, StudentQuestion | None]:
    checkpoint = snapshot.checkpoint
    if checkpoint is None:
        return snapshot, None
    if not any(question.answer is None for question in checkpoint.questions):
        if len(checkpoint.questions) >= snapshot.policy.question_budget:
            return snapshot, None
        skill = checkpoint.target
        if skill is None:
            ready = snapshot.progress.curriculum.next_up(snapshot.progress.mask)
            if not ready:
                return snapshot, None
            skill = ready[0].code
        result = store.execute(
            learner, AskQuestion(skill, len(checkpoint.questions)), Actor.ENGINE
        )
        if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
            raise StoreError("could not prepare the next question")
        snapshot = result.snapshot
        checkpoint = snapshot.checkpoint
    if checkpoint is None:
        return snapshot, None
    question = next(
        (candidate for candidate in checkpoint.questions if candidate.answer is None),
        None,
    )
    if question is None:
        return snapshot, None
    return snapshot, StudentQuestion(
        question.identifier,
        question_prompt(snapshot, question),
        question.instance.answer_type.value,
        question.instance.options,
    )


def _drain_engine(
    store: SQLiteRecordStore, learner: LearnerId, snapshot: Snapshot
) -> Snapshot:
    while True:
        action = TutorDesk.available(snapshot).engine_action
        if action is None:
            return snapshot
        result = store.execute(learner, action, Actor.ENGINE)
        if result.status is not ExecutionStatus.COMMITTED or result.snapshot is None:
            raise StoreError("could not finish the current check")
        snapshot = result.snapshot


def _answer_was_correct(snapshot: Snapshot, identifier: QuestionId) -> bool:
    checkpoint = snapshot.checkpoint
    if checkpoint is None:
        return False
    question = next(
        (
            candidate
            for candidate in checkpoint.questions
            if candidate.identifier == identifier
        ),
        None,
    )
    return (
        question is not None and question.answer is not None and question.answer.correct
    )


def _status_copy(
    snapshot: Snapshot, question: StudentQuestion | None, target_title: str | None
) -> str:
    if not snapshot.progress.placed:
        return "Let’s see where you are starting from."
    if question is not None:
        return "Quick check in progress"
    if target_title is not None:
        return f"Working on {target_title}"
    return "Choose a next step when you are ready."


def _path_titles(snapshot: Snapshot, requested: SkillCode) -> tuple[str, ...]:
    try:
        path = snapshot.progress.curriculum.path_to(snapshot.progress.mask, requested)
    except ValueError:
        return ()
    return tuple(item.title for item in path)


def _path_message(path: tuple[str, ...]) -> str:
    if not path:
        return "That choice is not available right now."
    return f"We’ll get there. First, follow this path: {', then '.join(path)}."


def _refusal_copy(result: ExecutionResult) -> str:
    if result.refusal is RefusalReason.CHECK_IN_PROGRESS:
        return "Finish the quick check already in progress first."
    if result.refusal is RefusalReason.PLACEMENT_REQUIRED:
        return "Start with a short check so we can find a good next step."
    if result.refusal is RefusalReason.ALREADY_MASTERED:
        return "You have already completed that skill."
    return "That step is not available right now."


def _sse_events(events: Iterator[InstructorEvent]) -> Iterator[str]:
    for event in events:
        payload: dict[str, str] = {}
        if event.text is not None:
            payload["text"] = event.text
        if event.outcome is not None:
            payload["outcome"] = event.outcome.value
        yield f"data: {dumps(payload, separators=(',', ':'))}\n\n"


def _static_events(text: str) -> Iterator[str]:
    yield f"data: {dumps({'text': text}, separators=(',', ':'))}\n\n"
    yield 'data: {"outcome":"completed"}\n\n'


def _lesson_url(learner: LearnerId, student_turn: str, requested: str) -> str:
    query = urlencode({"student_turn": student_turn, "requested_skill": requested})
    return f"/learners/{learner}/lesson/stream?{query}"


def _skill_title(snapshot: Snapshot, code: str) -> str:
    try:
        return next(
            skill.title
            for skill in snapshot.progress.curriculum.skills
            if skill.code == code
        )
    except StopIteration as error:
        raise StoreError("saved record references an unknown skill") from error


def _learner_or_none(value: str) -> LearnerId | None:
    try:
        return learner_id(value)
    except InvalidIdentifier:
        return None


def _form_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _render(
    templates: Jinja2Templates,
    request: Request,
    name: str,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


def _not_found(templates: Jinja2Templates, request: Request) -> HTMLResponse:
    return _render(
        templates,
        request,
        "not_found.html",
        {},
        status_code=404,
    )
