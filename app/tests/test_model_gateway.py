from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mali_app.model_gateway import (
    FixtureMissing,
    FixtureModelGateway,
    GatewayConfigurationError,
    GatewaySchemaViolation,
    GatewayTimeout,
    GatewayUnavailable,
    OpenAIModelGateway,
    RecordedFixture,
    RecordingModelGateway,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)


class WrittenItem(BaseModel):
    prompt: str
    answer: str


@dataclass
class StaticGateway:
    text: tuple[str, ...]
    item: WrittenItem

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        return (StreamDelta(text) for text in self.text)

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        return request.result_type.model_validate(self.item.model_dump(mode="json"))


def test_recorded_gateway_replays_streams_and_structured_results() -> None:
    stream_request = StreamRequest("Teach clearly.", "Help with halves.", 50)
    item_request = StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)
    recorder = RecordingModelGateway(
        StaticGateway(
            ("Start with ", "one half."), WrittenItem(prompt="1/2 + 1/2", answer="1")
        )
    )

    assert (
        "".join(delta.text for delta in recorder.stream(stream_request))
        == "Start with one half."
    )
    assert recorder.structured(item_request) == WrittenItem(
        prompt="1/2 + 1/2", answer="1"
    )

    replay = FixtureModelGateway(recorder.fixtures)
    assert (
        "".join(delta.text for delta in replay.stream(stream_request))
        == "Start with one half."
    )
    assert replay.structured(item_request) == WrittenItem(
        prompt="1/2 + 1/2", answer="1"
    )


def test_fixture_gateway_reports_missing_or_invalid_structured_fixtures() -> None:
    request = StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)
    missing = FixtureModelGateway(())
    with pytest.raises(FixtureMissing):
        missing.structured(request)

    recorded = RecordingModelGateway(
        StaticGateway(("unused",), WrittenItem(prompt="1/2 + 1/2", answer="1"))
    )
    recorded.structured(request)
    invalid = FixtureModelGateway(
        (RecordedFixture(recorded.fixtures[0].fingerprint, structured={"prompt": "x"}),)
    )
    with pytest.raises(GatewaySchemaViolation):
        invalid.structured(request)


def test_live_gateway_uses_responses_streaming_and_structured_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    responses = FakeResponses()
    client = FakeClient(responses)
    gateway = OpenAIModelGateway(client=client)
    stream_request = StreamRequest("Teach clearly.", "Help with halves.", 50)
    item_request = StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)

    assert (
        "".join(delta.text for delta in gateway.stream(stream_request)) == "One half."
    )
    assert gateway.structured(item_request) == WrittenItem(
        prompt="1/2 + 1/2", answer="1"
    )
    assert responses.create_calls[0]["stream"] is True
    assert responses.create_calls[0]["store"] is False
    assert responses.create_calls[0]["model"] == "gpt-5.6"
    assert responses.parse_calls[0]["text_format"] is WrittenItem


def test_live_gateway_requires_an_environment_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(GatewayConfigurationError):
        OpenAIModelGateway()


def test_live_gateway_retries_a_transient_timeout_before_streaming() -> None:
    responses = FakeResponses(
        stream_results=[
            TimeoutError(),
            iter((SimpleNamespace(type="response.output_text.delta", delta="Ready."),)),
        ]
    )
    gateway = OpenAIModelGateway(client=FakeClient(responses), retry_attempts=2)

    result = "".join(
        delta.text
        for delta in gateway.stream(StreamRequest("Teach clearly.", "Help.", 50))
    )

    assert result == "Ready."
    assert len(responses.create_calls) == 2


def test_live_gateway_returns_a_typed_timeout_after_bounded_retries() -> None:
    responses = FakeResponses(stream_results=[TimeoutError(), TimeoutError()])
    gateway = OpenAIModelGateway(client=FakeClient(responses), retry_attempts=2)

    with pytest.raises(GatewayTimeout):
        tuple(gateway.stream(StreamRequest("Teach clearly.", "Help.", 50)))

    assert len(responses.create_calls) == 2


def test_live_gateway_rejects_a_failed_stream_as_unavailable() -> None:
    responses = FakeResponses(
        stream_results=[iter((SimpleNamespace(type="response.failed"),))]
    )
    gateway = OpenAIModelGateway(client=FakeClient(responses), retry_attempts=1)

    with pytest.raises(GatewayUnavailable):
        tuple(gateway.stream(StreamRequest("Teach clearly.", "Help.", 50)))


class FakeResponses:
    def __init__(self, *, stream_results: list[object] | None = None) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.parse_calls: list[dict[str, object]] = []
        self._stream_results = stream_results or [
            iter(
                (
                    SimpleNamespace(type="response.created"),
                    SimpleNamespace(type="response.output_text.delta", delta="One "),
                    SimpleNamespace(type="response.output_text.delta", delta="half."),
                )
            )
        ]

    def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        result = self._stream_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def parse(self, **kwargs: object) -> object:
        self.parse_calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=WrittenItem(prompt="1/2 + 1/2", answer="1")
        )


@dataclass
class FakeClient:
    _responses: FakeResponses

    @property
    def responses(self) -> FakeResponses:
        return self._responses
