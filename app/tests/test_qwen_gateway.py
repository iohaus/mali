"""Offline contracts for the Qwen Cloud adapter."""

from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import BaseModel

from mali_app.model_gateway import FunctionTool, StreamRequest, StructuredRequest
from mali_app.qwen_gateway import QwenModelGateway


class WrittenItem(BaseModel):
    question_text: str


def test_qwen_gateway_uses_json_mode_for_structured_output() -> None:
    chat = FakeChatCompletions('{"question_text":"What is one half plus one half?"}')
    gateway = QwenModelGateway(client=FakeQwenClient(FakeResponses(), FakeChat(chat)))

    result = gateway.structured(
        StructuredRequest("Write one item.", "Use halves.", 50, WrittenItem)
    )

    assert result == WrittenItem(question_text="What is one half plus one half?")
    assert chat.calls[0]["response_format"] == {"type": "json_object"}
    assert chat.calls[0]["extra_body"] == {"enable_thinking": False}
    assert "JSON object" in chat.calls[0]["messages"][0]["content"]


def test_qwen_gateway_uses_responses_streaming_without_strict_tools() -> None:
    responses = FakeResponses()
    gateway = QwenModelGateway(
        client=FakeQwenClient(responses, FakeChatCompletions("{}"))
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
    assert responses.calls[0]["tools"] == [
        {
            "type": "function",
            "name": "get_progress_summary",
            "description": "Read the current progress summary.",
            "parameters": tool.parameters,
        }
    ]
    assert gateway.identity.trace_label == "qwen:qwen3.7-plus"


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return iter(
            (SimpleNamespace(type="response.output_text.delta", delta="Ready."),)
        )

    def parse(self, **kwargs: object) -> object:
        raise AssertionError("Qwen structured output uses chat JSON mode")


class FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


@dataclass
class FakeChat:
    completions: FakeChatCompletions


@dataclass
class FakeQwenClient:
    responses: FakeResponses
    chat: FakeChat
