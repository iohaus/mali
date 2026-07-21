"""Offline contracts for the OpenAI-compatible fallback gateway."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from pydantic import BaseModel

from mali_app._compat_gateway import CompatModelGateway
from mali_app.model_gateway import FunctionTool, StreamRequest, StructuredRequest

_TEST_MODEL = "compat-test-model"
_TEST_BASE_URL = "https://compat.example.test/v1"


class WrittenItem(BaseModel):
    question_text: str


def test_compat_gateway_uses_json_mode_for_structured_output() -> None:
    chat = FakeChatCompletions(
        '{"question_text":"What is one half plus one half?"}')
    gateway = CompatModelGateway(
        model=_TEST_MODEL,
        base_url=_TEST_BASE_URL,
        client=FakeCompatClient(FakeResponses(), FakeChat(chat)),
    )

    result = gateway.structured(
        StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)
    )

    assert result == WrittenItem(
        question_text="What is one half plus one half?")
    call = cast(dict[str, Any], chat.calls[0])
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"enable_thinking": True}
    assert call["temperature"] == 0.2
    messages = cast(list[dict[str, Any]], call["messages"])
    assert "JSON object" in cast(str, messages[0]["content"])


def test_compat_gateway_accepts_fenced_json_despite_json_mode() -> None:
    chat = FakeChatCompletions(
        '```json\n{"question_text":"How many halves?"}\n```')
    gateway = CompatModelGateway(
        model=_TEST_MODEL,
        base_url=_TEST_BASE_URL,
        client=FakeCompatClient(FakeResponses(), FakeChat(chat)),
    )

    result = gateway.structured(
        StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)
    )

    assert result == WrittenItem(question_text="How many halves?")


def test_compat_gateway_uses_responses_streaming_without_strict_tools() -> None:
    responses = FakeResponses()
    gateway = CompatModelGateway(
        model=_TEST_MODEL,
        base_url=_TEST_BASE_URL,
        client=FakeCompatClient(
            responses, FakeChat(FakeChatCompletions("{}"))),
    )
    tool = FunctionTool(
        "get_progress_summary",
        "Read the current progress summary.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    text = "".join(
        delta.text
        for delta in gateway.stream(StreamRequest("Teach.", "Help.", 50, (tool,)))
    )

    assert text == "Ready."
    response_call = cast(dict[str, Any], responses.calls[0])
    assert response_call["tools"] == [
        {
            "type": "function",
            "name": "get_progress_summary",
            "description": "Read the current progress summary.",
            "parameters": tool.parameters,
        }
    ]
    assert gateway.identity.trace_label == f"compat:{_TEST_MODEL}"


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return iter(
            (SimpleNamespace(type="response.output_text.delta", delta="Ready."),)
        )

    def parse(self, **kwargs: object) -> object:
        raise AssertionError("Compat structured output uses chat JSON mode")


class FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self._content))]
        )


@dataclass
class FakeChat:
    completions: FakeChatCompletions


@dataclass
class FakeCompatClient:
    responses: FakeResponses
    chat: FakeChat
