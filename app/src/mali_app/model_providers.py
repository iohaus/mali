"""Provider selection and extension points for Mali's model gateway."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from os import environ
from typing import Protocol, cast
from urllib.parse import urlparse

import mali_app._compat_gateway as _compat_gateway
from mali_app.model_gateway import (
    DEFAULT_MODEL,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    GatewayConfigurationError,
    ModelGateway,
    OpenAIModelGateway,
)

DEFAULT_MODEL_PROVIDER = "openai"
_MODEL_PROVIDER_ENVIRONMENT_NAME = "MALI_MODEL_PROVIDER"
_MODEL_ENVIRONMENT_NAME = "MALI_MODEL"
_MODEL_BASE_URL_ENVIRONMENT_NAME = "MALI_MODEL_BASE_URL"
_MODEL_API_KEY_ENVIRONMENT_NAME = "MALI_MODEL_API_KEY"
_PROVIDER_ENTRY_POINT_GROUP = "mali.model_providers"


@dataclass(frozen=True, slots=True)
class ModelGatewaySettings:
    """Provider-neutral settings supplied at the application's composition root."""

    provider: str = DEFAULT_MODEL_PROVIDER
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise GatewayConfigurationError("model provider must not be blank")
        if self.model is not None and not self.model.strip():
            raise GatewayConfigurationError("model name must not be blank")
        _validate_base_url(self.base_url)
        if self.api_key is not None and not self.api_key.strip():
            raise GatewayConfigurationError("model API key must not be blank")
        if self.timeout_seconds <= 0:
            raise GatewayConfigurationError("gateway timeout must be positive")
        if self.retry_attempts < 1:
            raise GatewayConfigurationError("gateway attempts must be positive")

    @classmethod
    def from_environment(cls) -> ModelGatewaySettings:
        """Read provider and model selection without reading provider credentials.

        When MALI_MODEL_PROVIDER is absent the method checks for a DashScope
        credential and silently prefers the internal compat backend; otherwise
        it falls back to the primary OpenAI provider.
        """
        explicit_provider = environ.get(_MODEL_PROVIDER_ENVIRONMENT_NAME)
        if explicit_provider is not None:
            provider = explicit_provider
        elif environ.get("DASHSCOPE_API_KEY"):
            # Internal fallback: use the compat backend when the primary
            # credential is absent but a local alternative is available.
            provider = "compat"
        else:
            provider = DEFAULT_MODEL_PROVIDER
        model = environ.get(_MODEL_ENVIRONMENT_NAME)
        base_url = environ.get(_MODEL_BASE_URL_ENVIRONMENT_NAME)
        api_key = environ.get(_MODEL_API_KEY_ENVIRONMENT_NAME)
        return cls(
            provider=provider.lower(),
            model=model,
            base_url=base_url,
            api_key=api_key,
        )


class ModelGatewayProvider(Protocol):
    """One provider adapter factory discovered or registered by the composition root."""

    @property
    def name(self) -> str:
        """Return the unique, lowercase provider identifier."""
        ...

    def create(self, settings: ModelGatewaySettings) -> ModelGateway:
        """Build a gateway and read only this provider's credentials."""
        ...


class OpenAIModelProvider:
    """Built-in provider factory for the OpenAI Python SDK adapter."""

    @property
    def name(self) -> str:
        """Return the provider selector accepted by the registry."""
        return DEFAULT_MODEL_PROVIDER

    def create(self, settings: ModelGatewaySettings) -> ModelGateway:
        """Create the existing OpenAI adapter with its provider-neutral settings."""
        return OpenAIModelGateway(
            model=settings.model or DEFAULT_MODEL,
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
            retry_attempts=settings.retry_attempts,
        )


class CompatModelProvider:
    """Built-in provider factory for the internal OpenAI-compatible adapter."""

    @property
    def name(self) -> str:
        """Return the provider selector accepted by the registry."""
        return "compat"

    def create(self, settings: ModelGatewaySettings) -> ModelGateway:
        """Create the compat gateway; model and base_url are resolved from env."""
        api_key = settings.api_key or environ.get("DASHSCOPE_API_KEY")
        if api_key is None:
            raise GatewayConfigurationError(
                "DASHSCOPE_API_KEY or MALI_MODEL_API_KEY must be set for the "
                "compat provider"
            )
        model = settings.model or environ.get("MALI_COMPAT_MODEL")
        if not model:
            raise GatewayConfigurationError(
                "MALI_COMPAT_MODEL must be set when using the compat provider"
            )
        base_url = settings.base_url or environ.get("MALI_COMPAT_BASE_URL")
        if not base_url:
            raise GatewayConfigurationError(
                "MALI_COMPAT_BASE_URL must be set when using the compat provider"
            )
        return _compat_gateway.CompatModelGateway(
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=settings.timeout_seconds,
            retry_attempts=settings.retry_attempts,
        )


class ModelGatewayRegistry:
    """Registry that keeps provider discovery and SDK construction out of app routes."""

    def __init__(self, providers: Iterable[ModelGatewayProvider] = ()) -> None:
        self._providers: dict[str, ModelGatewayProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ModelGatewayProvider) -> None:
        """Register one provider factory, rejecting ambiguous provider names."""
        name = provider.name.lower()
        if not name or name != provider.name:
            raise GatewayConfigurationError(
                "model provider names must be non-empty and lowercase"
            )
        if name in self._providers:
            raise GatewayConfigurationError(
                f"model provider already registered: {name}"
            )
        self._providers[name] = provider

    def create(self, settings: ModelGatewaySettings) -> ModelGateway:
        """Resolve the selected provider and construct its gateway."""
        provider_name = settings.provider.lower()
        try:
            provider = self._providers[provider_name]
        except KeyError as error:
            available = ", ".join(sorted(self._providers)) or "none"
            raise GatewayConfigurationError(
                f"unknown model provider {provider_name!r}; available: {available}"
            ) from error
        return provider.create(settings)

    @property
    def providers(self) -> tuple[str, ...]:
        """Return registered provider names for diagnostics and configuration errors."""
        return tuple(sorted(self._providers))


def create_model_gateway_from_environment() -> ModelGateway:
    """Create the selected provider gateway from explicit process configuration."""
    settings = ModelGatewaySettings.from_environment()
    return default_model_gateway_registry().create(settings)


def default_model_gateway_registry() -> ModelGatewayRegistry:
    """Build the built-in registry plus installed provider plugins.

    Third-party packages can register a factory under the ``mali.model_providers``
    entry-point group. A factory must return a ``ModelGatewayProvider`` whose name
    matches the entry-point name.
    """
    registry = ModelGatewayRegistry((OpenAIModelProvider(), CompatModelProvider()))
    for entry_point in entry_points(group=_PROVIDER_ENTRY_POINT_GROUP):
        provider = cast(ModelGatewayProvider, entry_point.load()())
        if provider.name != entry_point.name:
            raise GatewayConfigurationError(
                "model provider entry point name must match its provider name: "
                f"{entry_point.name}"
            )
        registry.register(provider)
    return registry


def _validate_base_url(base_url: str | None) -> None:
    """Require a complete HTTP(S) endpoint when an override is supplied."""
    if base_url is None:
        return
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GatewayConfigurationError(
            "model base URL must be an absolute HTTP(S) URL"
        )
