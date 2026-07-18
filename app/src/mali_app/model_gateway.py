"""OpenAI Responses adapter and replayable fixtures for model-facing flows."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from os import environ
from typing import Protocol, cast

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ATTEMPTS = 2
_API_KEY_ENVIRONMENT_NAME = "OPENAI_API_KEY"


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
class StreamRequest:
    """One bounded request for streamed teaching text."""

    instructions: str
    input: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        _validate_request(self.instructions, self.input, self.max_output_tokens)


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
    """One visible text fragment from a streamed model result."""

    text: str


@dataclass(frozen=True, slots=True)
class RecordedFixture:
    """A request fingerprint and its deterministic offline result."""

    fingerprint: str
    stream: tuple[str, ...] | None = None
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
    """Call GPT-5.6 through the OpenAI Responses API with bounded retries."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        client: _OpenAIClient | None = None,
    ) -> None:
        if not model:
            raise GatewayConfigurationError("model name must not be blank")
        if timeout_seconds <= 0:
            raise GatewayConfigurationError("gateway timeout must be positive")
        if retry_attempts < 1:
            raise GatewayConfigurationError("gateway attempts must be positive")
        self._model = model
        self._retry_attempts = retry_attempts
        self._client = _openai_client(timeout_seconds) if client is None else client

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Yield typed text fragments from one live model request."""
        emitted = False
        for attempt in range(self._retry_attempts):
            try:
                response = self._client.responses.create(
                    model=self._model,
                    instructions=request.instructions,
                    input=request.input,
                    max_output_tokens=request.max_output_tokens,
                    store=False,
                    stream=True,
                )
                for event in _as_iterator(response):
                    text = _stream_text(event)
                    if text is not None:
                        emitted = True
                        yield StreamDelta(text)
                return
            except Exception as error:
                gateway_error = _gateway_error(error)
                if emitted or attempt == self._retry_attempts - 1:
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
                if attempt == self._retry_attempts - 1:
                    raise gateway_error from error
        raise AssertionError("bounded gateway loop must return or raise")


class FixtureModelGateway:
    """Replay recorded model responses without network access or credentials."""

    def __init__(self, fixtures: Sequence[RecordedFixture]) -> None:
        self._fixtures = {fixture.fingerprint: fixture for fixture in fixtures}

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Yield the recorded text fragments for a matching stream request."""
        fixture = self._fixture_for(_stream_fingerprint(request))
        if fixture.stream is None:
            raise FixtureMissing("fixture has a structured result, not a stream")
        return (StreamDelta(text) for text in fixture.stream)

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

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        """Forward a stream and record its complete visible text fragments."""
        fragments = tuple(delta.text for delta in self._gateway.stream(request))
        self._fixtures.append(
            RecordedFixture(_stream_fingerprint(request), stream=fragments)
        )
        return (StreamDelta(text) for text in fragments)

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
    """The swappable application boundary for GPT-backed tutoring flows."""

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]: ...

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT: ...


def _openai_client(timeout_seconds: float) -> _OpenAIClient:
    if not environ.get(_API_KEY_ENVIRONMENT_NAME):
        raise GatewayConfigurationError(
            f"{_API_KEY_ENVIRONMENT_NAME} must be set for live model calls"
        )
    try:
        from openai import OpenAI
    except ImportError as error:
        raise GatewayConfigurationError(
            "install the OpenAI SDK before using the live model gateway"
        ) from error
    return cast(_OpenAIClient, OpenAI(timeout=timeout_seconds, max_retries=0))


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


def _stream_text(event: object) -> str | None:
    event_type = _attribute(event, "type")
    if event_type in {"error", "response.failed", "response.incomplete"}:
        raise GatewayUnavailable("model stream ended before a completed response")
    if event_type != "response.output_text.delta":
        return None
    delta = _attribute(event, "delta")
    if not isinstance(delta, str):
        raise GatewayUnavailable("model stream contained an unreadable text fragment")
    return delta


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


def _stream_fingerprint(request: StreamRequest) -> str:
    return _fingerprint(
        "stream", request.instructions, request.input, str(request.max_output_tokens)
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
