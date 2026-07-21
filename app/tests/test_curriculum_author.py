"""The authoring flow must ship only curricula the core fully validates."""

from collections.abc import Iterator

import pytest
from mali.ids import learner_id
from pydantic import BaseModel, ValidationError

from mali_app.curriculum_author import (
    _EXAMPLE_SKILL,  # pyright: ignore[reportPrivateUsage]
    CurriculumAuthor,
    CurriculumBuildError,
    _certified_curriculum,  # pyright: ignore[reportPrivateUsage]
    _CurriculumDraft,  # pyright: ignore[reportPrivateUsage]
)
from mali_app.model_gateway import (
    GatewaySchemaViolation,
    GatewayTimeout,
    ModelIdentity,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)

LEARNER = learner_id("author-learner")


def _skill(code: str, requires: list[str]) -> dict[str, object]:
    return {
        "code": code,
        "title": f"Skill {code}",
        "explanation": (
            "Count the minutes carefully. Multiply the hours by sixty to convert "
            "them. Add any leftover minutes to finish the total."
        ),
        "requires": requires,
        "question": {
            "story": "How many minutes are in {h} hours?",
            "answer_rule": "h * 60",
            "answer_form": "integer",
            "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
        },
    }


def _draft(skills: list[dict[str, object]]) -> dict[str, object]:
    return {
        "title": "Telling Time",
        "summary": "Read clocks with confidence and convert between units.",
        "skills": skills,
    }


def _good_draft() -> dict[str, object]:
    return _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            _skill("last-steps", ["middle-steps"]),
        ]
    )


class ScriptedAuthorGateway:
    """Replay one recorded draft (or failure) per structured request."""

    identity = ModelIdentity("fixture", "author")

    def __init__(self, results: list[object]) -> None:
        self._results = results
        self.inputs: list[str] = []

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        raise AssertionError("the authoring flow never streams")

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        self.inputs.append(request.input)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return request.result_type.model_validate(result)


def test_valid_draft_becomes_a_core_validated_curriculum() -> None:
    gateway = ScriptedAuthorGateway([_good_draft()])
    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert authored.title == "Telling Time"
    assert authored.topic == "telling time"
    assert authored.model == "fixture:author"
    assert [skill.code for skill in authored.curriculum.skills] == [
        "first-steps",
        "middle-steps",
        "last-steps",
    ]
    assert all(skill.template is not None for skill in authored.curriculum.skills)
    assert authored.curriculum.prerequisites_for(
        authored.curriculum.skills[2].code
    ) == ("first-steps", "middle-steps")
    assert "<learner-request>\ntelling time\n</learner-request>" in gateway.inputs[0]


def test_rejected_draft_is_repaired_with_the_core_reason() -> None:
    cyclical = _draft(
        [
            _skill("first-steps", ["last-steps"]),
            _skill("middle-steps", ["first-steps"]),
            _skill("last-steps", ["middle-steps"]),
        ]
    )
    gateway = ScriptedAuthorGateway([cyclical, _good_draft()])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 2
    assert "Your previous draft was rejected" in gateway.inputs[1]
    assert "first-steps" in gateway.inputs[1]
    assert len(authored.curriculum.skills) == 3


def test_exhausted_unusable_drafts_fail_with_honest_learner_copy() -> None:
    unusable = _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": "How many minutes are in {h} hours?",
                    "answer_rule": "h * 60",
                    "answer_form": "essay",
                    "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([unusable, unusable, unusable])

    with pytest.raises(CurriculumBuildError) as failure:
        CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert "count, measure, or compute" in str(failure.value)
    assert len(gateway.inputs) == 3


def test_schema_violation_repair_carries_the_field_level_detail() -> None:
    schema_failure: GatewaySchemaViolation | None = None
    try:
        _CurriculumDraft.model_validate({"title": "Telling Time"})
        raise AssertionError("expected schema validation to fail")
    except ValidationError as error:
        schema_failure = GatewaySchemaViolation("model result did not match")
        schema_failure.__cause__ = error
    assert schema_failure is not None
    gateway = ScriptedAuthorGateway([schema_failure, _good_draft()])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 2
    assert "did not match the required schema" in gateway.inputs[1]
    assert "summary" in gateway.inputs[1]
    assert "skills" in gateway.inputs[1]
    assert len(authored.curriculum.skills) == 3


def test_gateway_outage_fails_without_a_repair_attempt() -> None:
    gateway = ScriptedAuthorGateway([GatewayTimeout("recorded timeout")])

    with pytest.raises(CurriculumBuildError) as failure:
        CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert "try again" in str(failure.value).lower()
    assert len(gateway.inputs) == 1


def test_missing_gateway_and_bad_topics_fail_before_any_model_call() -> None:
    author = CurriculumAuthor(None)
    with pytest.raises(CurriculumBuildError):
        author.build(LEARNER, "telling time")

    gateway = ScriptedAuthorGateway([])
    connected = CurriculumAuthor(gateway)
    with pytest.raises(CurriculumBuildError):
        connected.build(LEARNER, " ")
    with pytest.raises(CurriculumBuildError):
        connected.build(LEARNER, "x" * 300)
    assert gateway.inputs == []


def test_unruly_parameter_ranges_are_normalized_not_rejected() -> None:
    unruly = _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": "How many minutes are in {h} hours and {m} minutes?",
                    "answer_rule": "h * 60 + m",
                    "answer_form": "integer",
                    "parameters": [
                        {"name": "h", "lowest": 999, "highest": 1},
                        {"name": "m", "lowest": 5, "highest": 5},
                    ],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([unruly])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 1
    template = authored.curriculum.skills[2].template
    assert template is not None
    for parameter in template.parameters:
        assert 2 <= len(parameter.values) <= 40
    assert all(min(parameter.values) >= 1 for parameter in template.parameters)


def test_story_placeholder_mistakes_are_named_for_repair() -> None:
    mismatched = _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": "How many minutes are in {hours} hours?",
                    "answer_rule": "h * 60",
                    "answer_form": "integer",
                    "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([mismatched, _good_draft()])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 2
    assert "references {hours}" in gateway.inputs[1]
    assert "parameters are: h" in gateway.inputs[1]
    assert len(authored.curriculum.skills) == 3


def test_format_slips_in_rules_and_stories_are_normalized() -> None:
    sloppy = _draft(
        [
            _skill("first-steps", []),
            {
                **_skill("middle-steps", ["first-steps"]),
                "question": {
                    "story": "A tricky one. Ready? How many minutes in {h} hours?",
                    "answer_rule": "total = {h} * 60",
                    "answer_form": "integer",
                    "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                },
            },
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": "Count the minutes in {h} hours.",
                    "answer_rule": "{h} * 60",
                    "answer_form": "integer",
                    "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([sloppy])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 1
    middle = authored.curriculum.skills[1].template
    last = authored.curriculum.skills[2].template
    assert middle is not None and last is not None
    assert middle.plain_template.count("?") == 1
    assert middle.plain_template.startswith("A tricky one. Ready.")
    assert last.plain_template.endswith("hours?")
    assert middle.key_expression == "h * 60"
    assert last.key_expression == "h * 60"


def test_the_prompt_example_skill_passes_certification() -> None:
    draft = _CurriculumDraft.model_validate(
        {
            "title": "Conversational Spanish",
            "summary": "Build everyday Spanish one habit at a time.",
            "skills": [
                _EXAMPLE_SKILL,
                _skill("middle-steps", ["greeting-practice"]),
                _skill("last-steps", ["middle-steps"]),
            ],
        }
    )

    curriculum = _certified_curriculum(draft)

    assert len(curriculum.skills) == 3


def test_final_attempt_asks_for_the_simplest_patterns() -> None:
    unusable = _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": "How many minutes are in {h} hours?",
                    "answer_rule": "h * 60",
                    "answer_form": "essay",
                    "parameters": [{"name": "h", "lowest": 2, "highest": 9}],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([unusable, unusable, unusable])

    with pytest.raises(CurriculumBuildError):
        CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert "final attempt" not in gateway.inputs[1]
    assert "final attempt" in gateway.inputs[2]
    assert "simplest question patterns" in gateway.inputs[2]


def test_remainder_rules_and_four_parameters_are_accepted() -> None:
    clock = _draft(
        [
            _skill("first-steps", []),
            _skill("middle-steps", ["first-steps"]),
            {
                **_skill("last-steps", ["middle-steps"]),
                "question": {
                    "story": (
                        "Add {a} plus {b} plus {c} hours to {h}:00 on a wall "
                        "clock. What hour does it show?"
                    ),
                    "answer_rule": "(h + a + b + c) % 12",
                    "answer_form": "integer",
                    "parameters": [
                        {"name": "h", "lowest": 1, "highest": 11},
                        {"name": "a", "lowest": 1, "highest": 3},
                        {"name": "b", "lowest": 1, "highest": 3},
                        {"name": "c", "lowest": 1, "highest": 3},
                    ],
                },
            },
        ]
    )
    gateway = ScriptedAuthorGateway([clock])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 1
    template = authored.curriculum.skills[2].template
    assert template is not None
    assert len(template.parameters) == 4


def test_choice_questions_build_and_foundations_order_first() -> None:
    day_parts: dict[str, object] = {
        "code": "day-parts",
        "title": "Parts of the Day",
        "explanation": (
            "A 24-hour clock splits the day into three broad stretches. "
            "Morning runs until 8, afternoon until 16, and evening after."
        ),
        "requires": [],
        "assumed": True,
        "question": {
            "story": "It is {h}:00 on a 24-hour clock. Which part of the day is it?",
            "answer_rule": "h // 8",
            "answer_form": "choice",
            "parameters": [{"name": "h", "lowest": 0, "highest": 23}],
            "options": ["morning", "afternoon", "evening"],
        },
    }
    draft = _draft(
        [
            _skill("first-steps", ["day-parts"]),
            _skill("middle-steps", ["first-steps"]),
            day_parts,
        ]
    )
    gateway = ScriptedAuthorGateway([draft])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert authored.assumed_codes == ("day-parts",)
    assert authored.curriculum.skills[0].code == "day-parts"
    template = authored.curriculum.skills[0].template
    assert template is not None
    assert template.options == ("morning", "afternoon", "evening")
    instance = template.instance(4)
    assert instance.key in instance.options


def test_choice_rule_outside_its_options_is_named_for_repair() -> None:
    broken = {
        **_skill("last-steps", ["middle-steps"]),
        "question": {
            "story": "It is {h}:00 on a 24-hour clock. Which part of the day is it?",
            "answer_rule": "h",
            "answer_form": "choice",
            "parameters": [{"name": "h", "lowest": 0, "highest": 23}],
            "options": ["morning", "afternoon", "evening"],
        },
    }
    draft = _draft(
        [_skill("first-steps", []),
         _skill("middle-steps", ["first-steps"]), broken]
    )
    gateway = ScriptedAuthorGateway([draft, _good_draft()])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 2
    assert "whole number from 0 to 2" in gateway.inputs[1]
    assert len(authored.curriculum.skills) == 3


def test_too_small_choice_ranges_are_rejected_not_grown() -> None:
    narrow = {
        **_skill("last-steps", ["middle-steps"]),
        "question": {
            "story": "The minute hand made {q} quarter-turns. How much has passed?",
            "answer_rule": "q - 1",
            "answer_form": "choice",
            "parameters": [{"name": "q", "lowest": 1, "highest": 3}],
            "options": ["a quarter", "half", "three quarters"],
        },
    }
    draft = _draft(
        [_skill("first-steps", []),
         _skill("middle-steps", ["first-steps"]), narrow]
    )
    gateway = ScriptedAuthorGateway([draft, _good_draft()])

    authored = CurriculumAuthor(gateway).build(LEARNER, "telling time")

    assert len(gateway.inputs) == 2
    assert "at least 8 combinations" in gateway.inputs[1]
    assert len(authored.curriculum.skills) == 3
