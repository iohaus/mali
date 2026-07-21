"""End-to-end checks for the server-rendered student and teacher surfaces."""

import re
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

from fastapi.testclient import TestClient
from mali.actions import Actor, OverrideMastery
from mali.ids import learner_id, skill_code
from mali.policy import POLICY_V2
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


class CurriculumFixtureGateway:
    """Return one formal curriculum draft through the structured boundary."""

    identity = ModelIdentity("fixture", "curriculum")

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        raise AssertionError("this fixture only authors curricula")

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        self.inputs.append(request.input)
        return request.result_type.model_validate(
            {
                "title": "Telling Time",
                "summary": "Read clocks with confidence and convert between units.",
                "skills": [
                    {
                        "code": "minutes-in-hours",
                        "title": "Minutes in hours",
                        "explanation": (
                            "An hour has 60 minutes. To convert hours to minutes, "
                            "multiply the hours by 60. This makes durations easy "
                            "to compare."
                        ),
                        "requires": [],
                        "question": {
                            "story": "How many minutes are in {h} hours?",
                            "answer_rule": "h * 60",
                            "answer_form": "integer",
                            "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                        },
                    },
                    {
                        "code": "minutes-past",
                        "title": "Minutes past the hour",
                        "explanation": (
                            "A clock shows hours and minutes together. Adding the "
                            "minutes to the whole hours gives one total measured "
                            "in minutes. Counting this way keeps schedules exact."
                        ),
                        "requires": ["minutes-in-hours"],
                        "question": {
                            "story": (
                                "A film starts {h} hours and {m} minutes after "
                                "noon. How many minutes after noon is that?"
                            ),
                            "answer_rule": "h * 60 + m",
                            "answer_form": "integer",
                            "parameters": [
                                {"name": "h", "lowest": 1, "highest": 4},
                                {"name": "m", "lowest": 5, "highest": 24},
                            ],
                        },
                    },
                    {
                        "code": "quarter-hours",
                        "title": "Quarter hours",
                        "explanation": (
                            "A quarter of an hour is 15 minutes. Counting quarter "
                            "hours is a quick way to reason about short waits. "
                            "Four quarters always make one whole hour."
                        ),
                        "requires": ["minutes-in-hours"],
                        "question": {
                            "story": "How many minutes are in {q} quarter-hours?",
                            "answer_rule": "q * 15",
                            "answer_form": "integer",
                            "parameters": [{"name": "q", "lowest": 2, "highest": 11}],
                        },
                    },
                ],
            }
        )


def _adopt_demo_curriculum(database: str, learner: str) -> None:
    SQLiteRecordStore(database).adopt_curriculum(
        learner_id(learner),
        demo_curriculum(),
        title="Fraction foundations",
        summary="Practice fractions one step at a time.",
    )


def test_student_page_runs_placement_lesson_and_inline_check(tmp_path: Path) -> None:
    database = str(tmp_path / "student.db")
    client = TestClient(create_app(database))

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

    returning = client.post(
        "/learners",
        data={"learner_id": "web-learner", "display_name": "Ada"},
        follow_redirects=False,
    )
    assert returning.status_code == 303
    assert returning.headers["location"] == "/learners/web-learner"

    fresh = client.get("/learners/web-learner")
    assert "What would you like to learn?" in fresh.text
    _adopt_demo_curriculum(database, "web-learner")

    welcome_back = client.get("/")
    assert "Continue where you left off" in welcome_back.text
    assert "Welcome back, Ada" in welcome_back.text
    assert "Fraction foundations" in welcome_back.text
    resumed = client.post(
        f"/learners/web-learner/topics/{demo_curriculum().version}",
        follow_redirects=False,
    )
    assert resumed.status_code == 303
    assert resumed.headers["location"] == "/learners/web-learner"
    missing_topic = client.post(
        "/learners/web-learner/topics/not-a-version", follow_redirects=False
    )
    assert missing_topic.status_code == 404

    switched = client.post("/session/switch", follow_redirects=False)
    assert switched.status_code == 303
    signed_out_home = client.get("/")
    assert "Continue where you left off" not in signed_out_home.text
    assert "Start with Mali" in signed_out_home.text
    client.post(
        "/learners",
        data={"learner_id": "second-learner", "display_name": "Grace"},
        follow_redirects=False,
    )
    second_home = client.get("/")
    assert "Welcome back, Grace" in second_home.text
    assert "Ada" not in second_home.text
    assert "Fraction foundations" not in second_home.text
    client.post(
        "/learners",
        data={"learner_id": "web-learner", "display_name": "Ada"},
        follow_redirects=False,
    )

    page = client.post("/learners/web-learner/placement")
    for _ in range(POLICY_V2.question_budget):
        page = _submit_current_answer(client, "web-learner", page.text, correctly=False)

    assert "Choose your next step" in page.text
    assert "Equal halves" in page.text
    assert 'class="skill-button skill-button-later" type="button" disabled' in page.text
    lesson = client.post("/learners/web-learner/targets/equal-halves")
    assert "Great choice" in lesson.text
    assert "data-lesson-url" in lesson.text

    started = client.post("/learners/web-learner/check")
    for _ in range(POLICY_V2.pass_rule.asked):
        started = _submit_current_answer(client, "web-learner", started.text)

    assert "Adding halves" in started.text
    assert "Nice work" in started.text


def test_later_choice_streams_a_grounded_plan_without_changing_focus(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "refusal.db")
    client = TestClient(create_app(database))
    _place_and_choose_equal_halves(client, database, "path-learner")

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
    database = str(tmp_path / "instructor-plan.db")
    gateway = PlanFixtureGateway()
    client = TestClient(
        create_app(database, enable_instructor=True, model_gateway=gateway)
    )
    _place_and_choose_equal_halves(client, database, "instructor-path-learner")

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


def test_curriculum_build_is_streamed_adopted_and_scoped_to_one_learner(
    tmp_path: Path,
) -> None:
    gateway = CurriculumFixtureGateway()
    client = TestClient(
        create_app(str(tmp_path / "curriculum.db"), model_gateway=gateway)
    )
    for learner in ("course-owner", "other-learner"):
        created = client.post(
            "/learners",
            data={"learner_id": learner, "display_name": learner},
            follow_redirects=False,
        )
        assert created.status_code == 303

    stream = client.get(
        "/learners/course-owner/curriculum/stream",
        params={"topic": "telling time"},
    )
    owner = client.get("/learners/course-owner")
    other = client.get("/learners/other-learner")

    assert stream.headers["content-type"].startswith("text/event-stream")
    assert 'event: status\ndata: {"state":"building"' in stream.text
    assert 'event: curriculum\ndata: {"topic":"telling time"' in stream.text
    assert '"outcome":"completed"' in stream.text
    assert "Telling Time" in owner.text
    assert "Minutes in hours" in owner.text
    assert "Find your starting point" in owner.text
    assert "Telling Time" not in other.text
    assert "What would you like to learn?" in other.text
    assert len(gateway.inputs) == 1
    assert "<learner-request>\ntelling time\n</learner-request>" in gateway.inputs[0]


def test_curriculum_surface_shows_processing_affordance_and_teacher_entry(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(str(tmp_path / "curriculum-ui.db")))
    home = client.get("/")
    created = client.post(
        "/learners",
        data={"learner_id": "curriculum-ui", "display_name": "Ada"},
        follow_redirects=False,
    )
    page = client.get("/learners/curriculum-ui")
    script = client.get("/static/student.js")

    assert created.status_code == 303
    assert "What would you like to learn?" in page.text
    assert 'id="curriculum-form"' in page.text
    assert "processing-dots" in script.text
    assert 'href="/teacher"' in home.text


def test_teacher_view_expands_a_mastery_claim_into_recorded_answers(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "teacher.db")
    client = TestClient(create_app(database))
    _place_and_choose_equal_halves(client, database, "teacher-learner")
    page = client.post("/learners/teacher-learner/check")
    for _ in range(POLICY_V2.pass_rule.asked):
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
    _place_and_choose_equal_halves(client, database, "override-learner")
    page = client.post("/learners/override-learner/check")
    for _ in range(POLICY_V2.pass_rule.asked):
        page = _submit_current_answer(client, "override-learner", page.text)
    store = SQLiteRecordStore(database)
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


def _place_and_choose_equal_halves(
    client: TestClient, database: str, learner: str
) -> None:
    created = client.post(
        "/learners",
        data={"learner_id": learner, "display_name": "Ada"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    _adopt_demo_curriculum(database, learner)
    page = client.post(f"/learners/{learner}/placement")
    for _ in range(POLICY_V2.question_budget):
        page = _submit_current_answer(client, learner, page.text, correctly=False)
    selected = client.post(f"/learners/{learner}/targets/equal-halves")
    assert "Great choice" in selected.text


def _submit_current_answer(
    client: TestClient, learner: str, page: str, *, correctly: bool = True
):
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
    answer = _answer_for(question_prompt) if correctly else "999"
    return client.post(
        f"/learners/{learner}/answers",
        data={"question_id": question_id, "answer": answer},
    )


def _answer_for(prompt: str) -> str:
    halves = re.fullmatch(r"What is (\d+)/2 \+ 1/2\?", prompt)
    assert halves is not None
    value = Fraction(int(halves.group(1)), 2) + Fraction(1, 2)
    return str(value.numerator) if value.denominator == 1 else str(value)


def test_navigation_identity_topic_chips_and_cross_surface_links(
    tmp_path: Path,
) -> None:
    from mali.curriculum import Curriculum, Skill
    from mali.templates import AnswerType, ParameterDomain, QuestionTemplate

    database = str(tmp_path / "nav.db")
    client = TestClient(create_app(database))
    client.post(
        "/learners",
        data={"learner_id": "nav-learner", "display_name": "Ngozi"},
        follow_redirects=False,
    )

    fresh = client.get("/learners/nav-learner")
    assert "Ngozi" in fresh.text
    assert "My topics" in fresh.text
    assert "Teacher view" in fresh.text
    assert "/session/switch" in fresh.text

    _adopt_demo_curriculum(database, "nav-learner")
    single_topic = client.get("/learners/nav-learner")
    assert "Your topics" not in single_topic.text

    pairs_template = QuestionTemplate(
        (ParameterDomain("count", tuple(range(1, 9))),),
        "count * 2",
        "How many shoes fit {count} pairs?",
        AnswerType.INTEGER,
    )
    pairs = Skill(
        skill_code("pairs"),
        0,
        "Pairs",
        "Two matching things make one pair.",
        pairs_template,
    )
    SQLiteRecordStore(database).adopt_curriculum(
        learner_id("nav-learner"),
        Curriculum.load((pairs,), ()),
        title="Counting pairs",
        summary="Count in twos with everyday objects.",
    )

    page = client.get("/learners/nav-learner")
    assert "Your topics" in page.text
    assert "topic-chip-active" in page.text
    assert f"/learners/nav-learner/topics/{demo_curriculum().version}" in page.text

    during_check = client.post("/learners/nav-learner/placement")
    assert "Your topics" not in during_check.text

    teacher = client.get("/teacher")
    assert "Student view" in teacher.text
    missing = client.get("/learners/no-such-learner")
    assert missing.status_code == 404
    assert "Open teacher view" in missing.text


def test_skipping_placement_starts_study_from_the_first_skill(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "skip.db")
    client = TestClient(create_app(database))
    client.post(
        "/learners",
        data={"learner_id": "skip-learner", "display_name": "Sade"},
        follow_redirects=False,
    )
    _adopt_demo_curriculum(database, "skip-learner")

    before = client.get("/learners/skip-learner")
    assert "Let’s see where I am" in before.text
    assert "Skip — start from the beginning" in before.text

    page = client.post("/learners/skip-learner/placement/skip")
    assert "Starting from the beginning" in page.text
    assert "Choose your next step" in page.text
    assert "Equal halves" in page.text

    lesson = client.post("/learners/skip-learner/targets/equal-halves")
    assert "Great choice" in lesson.text
    started = client.post("/learners/skip-learner/check")
    for _ in range(POLICY_V2.pass_rule.asked):
        started = _submit_current_answer(client, "skip-learner", started.text)
    assert "Equal halves" in started.text

    repeat = client.post("/learners/skip-learner/placement/skip")
    assert "Your starting point is already set." in repeat.text

    store = SQLiteRecordStore(database)
    snapshot = store.snapshot(learner_id("skip-learner"))
    assert snapshot.progress.placed
    assert snapshot.progress.mask == 1
    assert store.audit(learner_id("skip-learner")).valid
