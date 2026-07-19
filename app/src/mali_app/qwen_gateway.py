"""Qwen Cloud adapter for Mali's provider-neutral model contract."""

from __future__ import annotations

import json
import os
from typing import Protocol, cast

from pydantic import BaseModel, ValidationError

from mali_app.model_gateway import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    FunctionTool,
    GatewaySchemaViolation,
    ModelIdentity,
    OpenAIModelGateway,
    StructuredRequest,
    _gateway_error,
    _is_retryable,
    _log_request_failure,
)

DEFAULT_QWEN_MODEL = "qwen3.6-flash"
DEFAULT_QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
# Low temperature keeps JSON-mode drafts format-compliant; streamed teaching
# keeps the provider default so lessons stay lively.
_STRUCTURED_TEMPERATURE = 0.2


class _ChatCompletionsApi(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _ChatApi(Protocol):
    @property
    def completions(self) -> _ChatCompletionsApi: ...


class _QwenClient(Protocol):
    @property
    def chat(self) -> _ChatApi: ...


class QwenModelGateway(OpenAIModelGateway):
    """Use Qwen's Responses stream and Chat Completions JSON mode.

    Qwen Cloud supports the OpenAI-compatible Responses API for streamed text and
    function calls. Its documented guaranteed-JSON path is Chat Completions JSON
    mode, so structured item rendering is deliberately translated at this edge.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_QWEN_MODEL,
        base_url: str = DEFAULT_QWEN_BASE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        client: _QwenClient | None = None,
    ) -> None:
        resolved_model = os.environ.get("QWEN_MODEL", model)
        super().__init__(
            model=resolved_model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            client=cast(object, client),
        )
        self._identity = ModelIdentity("qwen", resolved_model)

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        """Validate Qwen JSON mode output against the original Pydantic schema."""
        client = cast(_QwenClient, self._client)
        for attempt in range(self._retry_attempts):
            try:
                response = client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": _structured_instructions(request),
                        },
                        {"role": "user", "content": request.input},
                    ],
                    max_tokens=request.max_output_tokens,
                    temperature=_STRUCTURED_TEMPERATURE,
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                )
                content = _message_content(response)
                try:
                    parsed = json.loads(_without_code_fences(content))
                except json.JSONDecodeError as error:
                    raise GatewaySchemaViolation(
                        "Qwen did not return a JSON object"
                    ) from error
                return request.result_type.model_validate(parsed)
            except ValidationError as error:
                raise GatewaySchemaViolation(
                    "Qwen result did not match the schema"
                ) from error
            except GatewaySchemaViolation:
                raise
            except Exception as error:
                gateway_error = _gateway_error(error)
                retryable = _is_retryable(error)
                _log_request_failure(
                    "structured",
                    self._model,
                    attempt + 1,
                    self._retry_attempts,
                    error,
                    retryable,
                )
                if not retryable or attempt == self._retry_attempts - 1:
                    raise gateway_error from error
        raise AssertionError("bounded gateway loop must return or raise")

    def _tool_payloads(
        self, tools: tuple[FunctionTool, ...]
    ) -> list[dict[str, object]]:
        """Use Qwen's documented function-tool shape without OpenAI strict mode."""
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]


def _structured_instructions[ResultT: BaseModel](
    request: StructuredRequest[ResultT],
) -> str:
    schema = json.dumps(
        request.result_type.model_json_schema(), sort_keys=True, separators=(",", ":")
    )
    return (
        f"{request.instructions}\n"
        "Return only one JSON object that validates against this schema. "
        f"Schema: {schema}"
    )


def _without_code_fences(content: str) -> str:
    """Accept fenced JSON some Qwen models emit despite JSON mode."""
    text = content.strip()
    if not text.startswith("```"):
        return text
    first_break = text.find("\n")
    if first_break < 0:
        return text
    body = text[first_break + 1 :]
    closing = body.rfind("```")
    return body[:closing].strip() if closing >= 0 else body.strip()


def _message_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise GatewaySchemaViolation("Qwen returned no chat completion choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise GatewaySchemaViolation("Qwen returned no JSON content")
    return content
