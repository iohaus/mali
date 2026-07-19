from collections.abc import Iterator
from fractions import Fraction

from mali.policy import POLICY_V1
from mali.templates import (
    AnswerType,
    ParameterDomain,
    QuestionInstance,
    QuestionTemplate,
)
from pydantic import BaseModel

from mali_app.item_writer import ItemWriter, ItemWriterResponse
from mali_app.model_gateway import (
    GatewayError,
    GatewaySchemaViolation,
    GatewayUnavailable,
    ModelIdentity,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)


class ScriptedFixtureGateway:
    identity = ModelIdentity("fixture", "item-writer")

    def __init__(self, responses: list[ItemWriterResponse | GatewayError]) -> None:
        self.responses = responses
        self.request_inputs: list[str] = []

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        return iter(())

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        self.request_inputs.append(request.input)
        response = self.responses.pop(0)
        if isinstance(response, GatewayError):
            raise response
        return request.result_type.model_validate(response.model_dump(mode="json"))


def _question() -> tuple[QuestionTemplate, QuestionInstance]:
    template = QuestionTemplate(
        (ParameterDomain("number", tuple(range(8))),),
        "number + 10",
        "What is {number} plus 10?",
        AnswerType.INTEGER,
    )
    instance = QuestionInstance(
        (("number", Fraction(1)),),
        "11",
        "What is 1 plus 10?",
        AnswerType.INTEGER,
        (),
        False,
    )
    return template, instance


def test_item_writer_uses_only_params_and_returns_a_valid_structured_rendering() -> (
    None
):
    template, instance = _question()
    gateway = ScriptedFixtureGateway([ItemWriterResponse(question_text=instance.text)])

    result = ItemWriter(gateway).render(POLICY_V1, template, instance)

    assert result.question_text == instance.text
    assert result.attempts == 1
    assert not result.used_fallback
    assert "- number: 1" in gateway.request_inputs[0]
    assert instance.key not in gateway.request_inputs[0]


def test_item_writer_retries_a_key_leak_then_uses_the_valid_fixture() -> None:
    template, instance = _question()
    gateway = ScriptedFixtureGateway(
        [
            ItemWriterResponse(question_text="What is 1 plus 10; the result is 11?"),
            ItemWriterResponse(question_text=instance.text),
        ]
    )

    result = ItemWriter(gateway).render(POLICY_V1, template, instance)

    assert result.question_text == instance.text
    assert result.attempts == 2
    assert result.rejection_reasons == ("question text reveals the answer",)
    assert "previous-rendering-feedback" in gateway.request_inputs[1]


def test_item_writer_falls_back_after_schema_and_dropped_value_fixtures() -> None:
    template, instance = _question()
    gateway = ScriptedFixtureGateway(
        [
            GatewaySchemaViolation("missing structured output"),
            ItemWriterResponse(question_text="What should the result be?"),
            ItemWriterResponse(question_text="What should the result be?"),
        ]
    )

    result = ItemWriter(gateway).render(POLICY_V1, template, instance)

    assert result.question_text == instance.text
    assert result.used_fallback
    assert result.attempts == POLICY_V1.flow_budget.item_writer_retries + 1
    assert result.rejection_reasons == (
        "GatewaySchemaViolation",
        "question text omits required values",
        "question text omits required values",
    )


def test_item_writer_falls_back_without_repeating_an_unavailable_gateway() -> None:
    template, instance = _question()
    gateway = ScriptedFixtureGateway([GatewayUnavailable("forbidden")])

    result = ItemWriter(gateway).render(POLICY_V1, template, instance)

    assert result.question_text == instance.text
    assert result.used_fallback
    assert result.gateway_failed
    assert result.attempts == 1
    assert result.rejection_reasons == ("GatewayUnavailable",)
