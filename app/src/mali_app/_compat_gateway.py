from __future__ import annotations

import json
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError

from mali_app.model_gateway import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    FunctionTool,
    GatewaySchemaViolation,
    ModelIdentity,
    OpenAIClient,
    OpenAIModelGateway,
    StructuredRequest,
    gateway_error as wrap_error,
    is_retryable,
    log_request_failure,
)

_STRUCTURED_TEMPERATURE = 0.2


class _ChatCompletionsApi(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _ChatApi(Protocol):
    @property
    def completions(self) -> _ChatCompletionsApi: ...


class _CompatClient(OpenAIClient, Protocol):
    @property
    def chat(self) -> _ChatApi: ...


class CompatModelGateway(OpenAIModelGateway):

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        client: _CompatClient | None = None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
            client=cast(Any, client),
        )
        # Label kept generic so it does not surface vendor names in traces.
        self._identity = ModelIdentity("compat", model)

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        """Validate JSON mode output against the original Pydantic schema."""
        client = cast(_CompatClient, self._client)
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
                    extra_body={"enable_thinking": True},
                )
                content = _message_content(response)
                try:
                    parsed = json.loads(_without_code_fences(content))
                except json.JSONDecodeError as error:
                    raise GatewaySchemaViolation(
                        "model did not return a JSON object"
                    ) from error
                return request.result_type.model_validate(parsed)
            except ValidationError as error:
                raise GatewaySchemaViolation(
                    "model result did not match the schema"
                ) from error
            except GatewaySchemaViolation:
                raise
            except Exception as error:
                exc = wrap_error(error)
                retryable = is_retryable(error)
                log_request_failure(
                    "structured",
                    self._model,
                    attempt + 1,
                    self._retry_attempts,
                    error,
                    retryable,
                )
                if not retryable or attempt == self._retry_attempts - 1:
                    raise exc from error
        raise AssertionError("bounded gateway loop must return or raise")

    def _tool_payloads(
        self, tools: tuple[FunctionTool, ...]
    ) -> list[dict[str, object]]:
        """Function-tool shape without OpenAI strict mode."""
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
    """Strip markdown code fences some models emit despite JSON mode."""
    text = content.strip()
    if not text.startswith("```"):
        return text
    first_break = text.find("\n")
    if first_break < 0:
        return text
    body = text[first_break + 1:]
    closing = body.rfind("```")
    return body[:closing].strip() if closing >= 0 else body.strip()


def _message_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise GatewaySchemaViolation(
            "provider returned no chat completion choices")
    first: object = cast(object, choices[0])
    message = getattr(first, "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise GatewaySchemaViolation("provider returned no JSON content")
    return content
