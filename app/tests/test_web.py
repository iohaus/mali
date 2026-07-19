"""End-to-end checks for the server-rendered student and teacher surfaces."""

import re
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

from fastapi.testclient import TestClient
from mali.actions import Actor, OverrideMastery
from mali.ids import learner_id, skill_code
from pydantic import BaseModel

from mali_app.api import create_app
from mali_app.demo import demo_curriculum
from mali_app.model_gateway import (
    ModelIdentity,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)
from mali_app.store import SQLiteRecordStore
from mali_app.store_types import ExecutionStatus


class PlanFixtureGateway:
    """Return one recorded tutoring response for the refusal journey."""

    identity = ModelIdentity("fixture", "plan")

    def __init__(self) -> None:
        self.requests: list[StreamRequest] = []

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        self.requests.append(request)
        return iter((StreamDelta(text="We’ll get there. Start with equal halves."),))

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        raise AssertionError("this fixture only teaches")


def test_student_page_runs_placement_lesson_and_inline_check(tmp_path: Path) -> None:
    client = TestClient(create_app(str(tmp_path / "student.db")))

    home = client.get("/")
    created = client.post(
        "/learners",
        data={"learner_id": "web-learner", "display_name": "Ada"},
        follow_redirects=False,
    )

    assert home.status_code == 200
    assert "Start with Mali" in home.text
    assert "htmx.org" in home.text
    assert created.status_code == 303
    assert created.headers["location"] == "/learners/web-learner"

    page = client.post("/learners/web-learner/placement")
    for _ in range(5):
        page = _submit_current_answer(client, "web-learner", page.text)

    assert "Choose your next step" in page.text
    assert "Equal halves" in page.text
    lesson = client.post("/learners/web-learner/targets/equal-halves")
    assert "Great choice" in lesson.text
    assert "data-lesson-url" in lesson.text

    started = client.post("/learners/web-learner/check")
    for _ in range(3):
        started = _submit_current_answer(client, "web-learner", started.text)

    assert "Adding halves" in started.text
    assert "Nice work" in started.text


def test_later_choice_streams_a_grounded_plan_without_changing_focus(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(str(tmp_path / "refusal.db")))
    _place_and_choose_equal_halves(client, "path-learner")

    choice = client.post("/learners/path-learner/targets/adding-quarters")
    stream = client.get(
        "/learners/path-learner/lesson/stream",
        params={
            "student_turn": "I want to work on adding quarters.",
            "requested_skill": "adding-quarters",
        },
    )
    page = client.get("/learners/path-learner")

    assert "We’ll get there" in choice.text
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "Your path" in stream.text
    assert "Adding halves" in stream.text
    assert "Working on Equal halves" in page.text


def test_later_choice_gives_the_instructor_a_path_for_its_streamed_plan(
    tmp_path: Path,
) -> None:
    gateway = PlanFixtureGateway()
    client = TestClient(
        create_app(
            str(tmp_path / "instructor-plan.db"),
            enable_instructor=True,
            model_gateway=gateway,
        )
    )
    _place_and_choose_equal_halves(client, "instructor-path-learner")

    choice = client.post("/learners/instructor-path-learner/targets/adding-quarters")
    stream = client.get(
        "/learners/instructor-path-learner/lesson/stream",
        params={
            "student_turn": "I want to work on adding quarters.",
            "requested_skill": "adding-quarters",
        },
    )

    assert "We’ll get there" in choice.text
    assert "Start with equal halves" in stream.text
    assert len(gateway.requests) == 1
    assert "prerequisite-path: Equal halves, Adding halves, Adding quarters" in (
        gateway.requests[0].input
    )


def test_teacher_view_expands_a_mastery_claim_into_recorded_answers(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(str(tmp_path / "teacher.db")))
    _place_and_choose_equal_halves(client, "teacher-learner")
    page = client.post("/learners/teacher-learner/check")
    for _ in range(3):
        page = _submit_current_answer(client, "teacher-learner", page.text)

    dashboard = client.get("/teacher")
    detail = client.get("/teacher/teacher-learner")

    assert dashboard.status_code == 200
    assert "Ada" in dashboard.text
    assert "Checked" in dashboard.text
    assert detail.status_code == 200
    assert "Mastered Equal halves" in detail.text
    assert "Student answer" in detail.text
    assert "What is" in detail.text
    assert "Correct" in detail.text


def test_teacher_view_includes_teacher_attribution_and_note_for_overrides(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "override.db")
    client = TestClient(create_app(database))
    _place_and_choose_equal_halves(client, "override-learner")
    page = client.post("/learners/override-learner/check")
    for _ in range(3):
        page = _submit_current_answer(client, "override-learner", page.text)
    store = SQLiteRecordStore(database, demo_curriculum())
    result = store.execute(
        learner_id("override-learner"),
        OverrideMastery(skill_code("adding-halves"), "Reviewed completed work."),
        Actor.TEACHER,
    )

    detail = client.get("/teacher/override-learner")

    assert result.status is ExecutionStatus.COMMITTED
    assert "Mastered Adding halves" in detail.text
    assert "Teacher note: Reviewed completed work." in detail.text
    assert "Teacher" in detail.text


def _place_and_choose_equal_halves(client: TestClient, learner: str) -> None:
    created = client.post(
        "/learners",
        data={"learner_id": learner, "display_name": "Ada"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    page = client.post(f"/learners/{learner}/placement")
    for _ in range(5):
        page = _submit_current_answer(client, learner, page.text)
    selected = client.post(f"/learners/{learner}/targets/equal-halves")
    assert "Great choice" in selected.text


def _submit_current_answer(client: TestClient, learner: str, page: str):
    match = re.search(
        r'name="question_id" value="([^"]+)"[^>]*>\s*'
        r"(?:<[^>]+>\s*)*<p class=\"question-prompt\">([^<]+)</p>",
        page,
    )
    if match is None:
        prompt = re.search(r'<p class="question-prompt">([^<]+)</p>', page)
        identifier = re.search(r'name="question_id" value="([^"]+)"', page)
        assert prompt is not None
        assert identifier is not None
        question_id, question_prompt = identifier.group(1), prompt.group(1)
    else:
        question_id, question_prompt = match.group(1), match.group(2)
    return client.post(
        f"/learners/{learner}/answers",
        data={"question_id": question_id, "answer": _answer_for(question_prompt)},
    )


def _answer_for(prompt: str) -> str:
    halves = re.fullmatch(r"What is (\d+)/2 \+ 1/2\?", prompt)
    assert halves is not None
    value = Fraction(int(halves.group(1)), 2) + Fraction(1, 2)
    return str(value.numerator) if value.denominator == 1 else str(value)
