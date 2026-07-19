"""OpenAI Responses adapter and replayable fixtures for model-facing flows."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from os import environ
from typing import Protocol, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ATTEMPTS = 2
_API_KEY_ENVIRONMENT_NAME = "OPENAI_API_KEY"
_LOG = logging.getLogger(__name__)


class GatewayError(Exception):
    """Base class for model-boundary failures with an actionable outcome."""


class GatewayConfigurationError(GatewayError):
    """Raised when the live gateway has no configured API credential."""


class GatewayTimeout(GatewayError):
    """Raised when a model call does not finish before its request deadline."""


class GatewayUnavailable(GatewayError):
    """Raised when the model service cannot complete a request."""


class GatewaySchemaViolation(GatewayError):
    """Raised when a structured response cannot satisfy its requested shape."""


class FixtureMissing(GatewayError):
    """Raised when an offline test requests a model result not in its fixtures."""


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Provider and model attribution retained with every teaching trace."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("model identity needs a provider and model name")

    @property
    def trace_label(self) -> str:
        """Return the stable value persisted in the teaching trace."""
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True, slots=True)
class StreamRequest:
    """One bounded request for streamed teaching text."""

    instructions: str
    input: str
    max_output_tokens: int
    tools: tuple[FunctionTool, ...] = ()

    def __post_init__(self) -> None:
        _validate_request(self.instructions, self.input, self.max_output_tokens)
        names = tuple(tool.name for tool in self.tools)
        if len(set(names)) != len(names):
            raise ValueError("model tool names must be unique")


@dataclass(frozen=True, slots=True)
class FunctionTool:
    """One JSON-schema function exposed to a streamed model turn."""

    name: str
    description: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError("model tool names must be valid identifiers")
        if not self.description.strip():
            raise ValueError("model tool descriptions must not be blank")
        if self.parameters.get("type") != "object":
            raise ValueError("model tool parameters must be an object schema")


@dataclass(frozen=True, slots=True)
class StructuredRequest[ResultT: BaseModel]:
    """One bounded request for a Pydantic-validated model result."""

    instructions: str
    input: str
    max_output_tokens: int
    result_type: type[ResultT]

    def __post_init__(self) -> None:
        _validate_request(self.instructions, self.input, self.max_output_tokens)


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """One text fragment or function-call event from a streamed model result."""

    text: str = ""
    tool_name: str | None = None
    tool_arguments: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        is_tool_call = self.tool_name is not None
        if is_tool_call != (self.tool_arguments is not None):
            raise ValueError("a model tool call needs both name and arguments")
        if self.text and is_tool_call:
            raise ValueError("a stream event cannot be text and a tool call")


@dataclass(frozen=True, slots=True)
class RecordedFixture:
    """A request fingerprint and its deterministic offline result."""

    fingerprint: str
    stream: tuple[StreamDelta, ...] | None = None
    structured: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if (self.stream is None) == (self.structured is None):
            raise ValueError("a fixture must contain exactly one result form")


class _ResponsesApi(Protocol):
    def create(self, **kwargs: object) -> object: ...

    def parse(self, **kwargs: object) -> object: ...


class _OpenAIClient(Protocol):
    @property
    def responses(self) -> _ResponsesApi: ...


class OpenAIModelGateway:
    """Call an OpenAI Responses model with bounded retries."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        client: _OpenAIClient | None = None,
    ) -> None:
        if not model:
            raise GatewayConfigurationError("model name must not be blank")
        _validate_base_url(base_url)
        if timeout_seconds <= 0:
            raise GatewayConfigurationError("gateway timeout must be positive")
        if retry_attempts < 1:
            raise GatewayConfigurationError("gateway attempts must be positive")
        self._model = model
        self._identity = ModelIdentity("openai", model)
        self._retry_attempts = retry_attempts
        self._client = (
            _openai_client(timeout_seconds, base_url, api_key)
            if client is None
            else client
        )

    @property
    def identity(self) -> ModelIdentity:
        """Return the OpenAI model selected for this gateway."""
        return self._identity

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Yield typed text fragments from one live model request."""
        emitted = False
        for attempt in range(self._retry_attempts):
            try:
                payload: dict[str, object] = {
                    "model": self._model,
                    "instructions": request.instructions,
                    "input": request.input,
                    "max_output_tokens": request.max_output_tokens,
                    "store": False,
                    "stream": True,
                }
                if request.tools:
                    payload["tools"] = self._tool_payloads(request.tools)
                response = self._client.responses.create(**payload)
                for event in _as_iterator(response):
                    delta = _stream_delta(event)
                    if delta is not None:
                        emitted = True
                        yield delta
                return
            except Exception as error:
                gateway_error = _gateway_error(error)
                retryable = _is_retryable(error)
                _log_request_failure(
                    "stream",
                    self._model,
                    attempt + 1,
                    self._retry_attempts,
                    error,
                    retryable,
                )
                if (
                    emitted
                    or not retryable
                    or attempt == self._retry_attempts - 1
                ):
                    raise gateway_error from error

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        """Return one Pydantic-validated Responses API result."""
        for attempt in range(self._retry_attempts):
            try:
                response = self._client.responses.parse(
                    model=self._model,
                    instructions=request.instructions,
                    input=request.input,
                    max_output_tokens=request.max_output_tokens,
                    store=False,
                    text_format=request.result_type,
                )
                parsed = _attribute(response, "output_parsed")
                if parsed is None:
                    raise GatewaySchemaViolation("model returned no structured result")
                if isinstance(parsed, request.result_type):
                    return parsed
                return request.result_type.model_validate(parsed)
            except ValidationError as error:
                raise GatewaySchemaViolation(
                    "model result did not match the schema"
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
        """Translate Mali's closed tool contract to OpenAI Responses tools."""
        return [_tool_payload(tool) for tool in tools]


class FixtureModelGateway:
    """Replay recorded model responses without network access or credentials."""

    def __init__(self, fixtures: Sequence[RecordedFixture]) -> None:
        self._fixtures = {fixture.fingerprint: fixture for fixture in fixtures}

    @property
    def identity(self) -> ModelIdentity:
        """Identify deterministic replay without claiming a live provider."""
        return ModelIdentity("fixture", "replay")

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Yield recorded text and function-call events for a matching request."""
        fixture = self._fixture_for(_stream_fingerprint(request))
        if fixture.stream is None:
            raise FixtureMissing("fixture has a structured result, not a stream")
        return iter(fixture.stream)

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        """Recreate the recorded structured result using the requested schema."""
        fixture = self._fixture_for(_structured_fingerprint(request))
        if fixture.structured is None:
            raise FixtureMissing("fixture has a stream result, not structured data")
        try:
            return request.result_type.model_validate(fixture.structured)
        except ValidationError as error:
            raise GatewaySchemaViolation(
                "fixture did not match the requested schema"
            ) from error

    def _fixture_for(self, fingerprint: str) -> RecordedFixture:
        try:
            return self._fixtures[fingerprint]
        except KeyError as error:
            raise FixtureMissing(f"no fixture matches request {fingerprint}") from error


class RecordingModelGateway:
    """Wrap a gateway and retain replay fixtures for each completed request."""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway
        self._fixtures: list[RecordedFixture] = []

    @property
    def fixtures(self) -> tuple[RecordedFixture, ...]:
        """Return fixtures in the order their source requests completed."""
        return tuple(self._fixtures)

    @property
    def identity(self) -> ModelIdentity:
        """Preserve attribution from the wrapped live or fixture gateway."""
        return self._gateway.identity

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Forward a stream and record all of its typed result events."""
        events = tuple(self._gateway.stream(request))
        self._fixtures.append(
            RecordedFixture(_stream_fingerprint(request), stream=events)
        )
        return iter(events)

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        """Forward a structured request and record the validated payload."""
        result = self._gateway.structured(request)
        self._fixtures.append(
            RecordedFixture(
                _structured_fingerprint(request),
                structured=cast(dict[str, object], result.model_dump(mode="json")),
            )
        )
        return result


class ModelGateway(Protocol):
    """Provider-neutral boundary for the tutoring model capabilities Mali needs."""

    @property
    def identity(self) -> ModelIdentity:
        """Return attribution for traces, logs, and provider-independent policy."""
        ...

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]: ...

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT: ...


def _openai_client(
    timeout_seconds: float,
    base_url: str | None = None,
    api_key: str | None = None,
) -> _OpenAIClient:
    resolved_api_key = api_key or environ.get(_API_KEY_ENVIRONMENT_NAME)
    if not resolved_api_key:
        raise GatewayConfigurationError(
            f"{_API_KEY_ENVIRONMENT_NAME} must be set for live model calls"
        )
    try:
        from openai import OpenAI
    except ImportError as error:
        raise GatewayConfigurationError(
            "install the OpenAI SDK before using the live model gateway"
        ) from error
    options: dict[str, object] = {
        "api_key": resolved_api_key,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    if base_url is not None:
        options["base_url"] = base_url
    return cast(_OpenAIClient, OpenAI(**options))


def _validate_base_url(base_url: str | None) -> None:
    """Reject malformed endpoint overrides before any provider SDK is loaded."""
    if base_url is None:
        return
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GatewayConfigurationError(
            "model base URL must be an absolute HTTP(S) URL"
        )


def _validate_request(
    instructions: str, input_text: str, max_output_tokens: int
) -> None:
    if not instructions.strip():
        raise ValueError("model instructions must not be blank")
    if not input_text.strip():
        raise ValueError("model input must not be blank")
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError("model output limit must be a positive integer")


def _as_iterator(value: object) -> Iterator[object]:
    try:
        return iter(cast(Iterator[object], value))
    except TypeError as error:
        raise GatewayUnavailable("model service did not return a stream") from error


def _stream_delta(event: object) -> StreamDelta | None:
    event_type = _attribute(event, "type")
    if event_type in {"error", "response.failed", "response.incomplete"}:
        raise GatewayUnavailable("model stream ended before a completed response")
    if event_type == "response.output_item.done":
        item = _attribute(event, "item")
        if _attribute(item, "type") != "function_call":
            return None
        name = _attribute(item, "name")
        arguments = _attribute(item, "arguments")
        call_id = _attribute(item, "call_id")
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise GatewayUnavailable("model function call was unreadable")
        return StreamDelta(
            tool_name=name,
            tool_arguments=arguments,
            tool_call_id=call_id if isinstance(call_id, str) else None,
        )
    if event_type != "response.output_text.delta":
        return None
    delta = _attribute(event, "delta")
    if not isinstance(delta, str):
        raise GatewayUnavailable("model stream contained an unreadable text fragment")
    return StreamDelta(delta)


def _attribute(value: object, name: str) -> object:
    try:
        return cast(object, getattr(value, name))
    except AttributeError as error:
        raise GatewayUnavailable(
            "model service returned an unreadable response"
        ) from error


def _gateway_error(error: Exception) -> GatewayError:
    if isinstance(error, GatewayError):
        return error
    name = type(error).__name__
    if name in {"APITimeoutError", "TimeoutError"}:
        return GatewayTimeout("model request timed out")
    if name in {
        "APIConnectionError",
        "APIStatusError",
        "InternalServerError",
        "RateLimitError",
    }:
        return GatewayUnavailable("model service is unavailable")
    return GatewayUnavailable("model service returned an unexpected error")


def _is_retryable(error: Exception) -> bool:
    """Return whether the failure can plausibly succeed on another attempt."""
    if isinstance(error, (GatewayTimeout, GatewayUnavailable)):
        return True
    if type(error).__name__ in {
        "APITimeoutError",
        "TimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "RateLimitError",
    }:
        return True
    status_code = _status_code(error)
    return status_code in {408, 409, 429} or (
        status_code is not None and status_code >= 500
    )


def _log_request_failure(
    operation: str,
    model: str,
    attempt: int,
    attempt_limit: int,
    error: Exception,
    retryable: bool,
) -> None:
    """Record request metadata without logging prompts or student content."""
    _LOG.warning(
        "model request failed operation=%s model=%s attempt=%s/%s "
        "error=%s status=%s retryable=%s",
        operation,
        model,
        attempt,
        attempt_limit,
        type(error).__name__,
        _status_code(error),
        retryable,
    )


def _status_code(error: Exception) -> int | None:
    status_code = getattr(error, "status_code", None)
    return status_code if type(status_code) is int else None


def _stream_fingerprint(request: StreamRequest) -> str:
    return _fingerprint(
        "stream",
        request.instructions,
        request.input,
        str(request.max_output_tokens),
        *(_tool_fingerprint(tool) for tool in request.tools),
    )


def _structured_fingerprint[ResultT: BaseModel](
    request: StructuredRequest[ResultT],
) -> str:
    return _fingerprint(
        "structured",
        request.instructions,
        request.input,
        str(request.max_output_tokens),
        request.result_type.__name__,
    )


def _fingerprint(*parts: str) -> str:
    return sha256("\x1f".join(parts).encode()).hexdigest()


def _tool_payload(tool: FunctionTool) -> dict[str, object]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "strict": True,
    }


def _tool_fingerprint(tool: FunctionTool) -> str:
    return json.dumps(_tool_payload(tool), sort_keys=True, separators=(",", ":"))
