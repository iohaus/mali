"""Provider-selection contracts independent of any provider SDK."""

from collections.abc import Iterator

import pytest
from pydantic import BaseModel

import mali_app.model_providers as model_providers
import mali_app._compat_gateway as _compat_gateway
from mali_app.model_gateway import (
    GatewayConfigurationError,
    ModelIdentity,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)
from mali_app.model_providers import (
    CompatModelProvider,
    ModelGatewayRegistry,
    ModelGatewaySettings,
    OpenAIModelProvider,
)


class ExampleGateway:
    identity = ModelIdentity("example", "instruct-v3")

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        return iter((StreamDelta("Ready."),))

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        raise AssertionError("provider selection does not invoke a model")


class ExampleProvider:
    name = "example"

    def __init__(self) -> None:
        self.settings: list[ModelGatewaySettings] = []

    def create(self, settings: ModelGatewaySettings) -> ExampleGateway:
        self.settings.append(settings)
        return ExampleGateway()


def test_registry_constructs_a_registered_provider_from_neutral_settings() -> None:
    provider = ExampleProvider()
    registry = ModelGatewayRegistry((provider,))
    settings = ModelGatewaySettings(provider="example", model="instruct-v3")

    gateway = registry.create(settings)

    assert gateway.identity.trace_label == "example:instruct-v3"
    assert provider.settings == [settings]
    assert registry.providers == ("example",)


def test_registry_names_available_providers_for_an_unknown_selection() -> None:
    registry = ModelGatewayRegistry((ExampleProvider(),))

    with pytest.raises(GatewayConfigurationError, match="available: example"):
        registry.create(ModelGatewaySettings(provider="missing"))


def test_settings_select_provider_and_model_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MALI_MODEL_PROVIDER", "example")
    monkeypatch.setenv("MALI_MODEL", "instruct-v3")
    monkeypatch.setenv("MALI_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("MALI_MODEL_API_KEY", "provider-key")

    assert ModelGatewaySettings.from_environment() == ModelGatewaySettings(
        provider="example",
        model="instruct-v3",
        base_url="https://models.example.test/v1",
        api_key="provider-key",
    )


def test_settings_reject_an_invalid_base_url() -> None:
    with pytest.raises(GatewayConfigurationError, match="base URL"):
        ModelGatewaySettings(base_url="models.example.test/v1")


def test_openai_provider_passes_base_url_to_its_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_gateway(**kwargs: object) -> ExampleGateway:
        received.update(kwargs)
        return ExampleGateway()

    monkeypatch.setattr(model_providers, "OpenAIModelGateway", fake_gateway)
    settings = ModelGatewaySettings(
        model="compatible-model",
        base_url="https://models.example.test/v1",
        timeout_seconds=12.5,
        retry_attempts=3,
    )

    gateway = OpenAIModelProvider().create(settings)

    assert gateway.identity.trace_label == "example:instruct-v3"
    assert received == {
        "model": "compatible-model",
        "base_url": "https://models.example.test/v1",
        "api_key": None,
        "timeout_seconds": 12.5,
        "retry_attempts": 3,
    }


def test_compat_provider_uses_the_generic_key_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_gateway(**kwargs: object) -> ExampleGateway:
        received.update(kwargs)
        return ExampleGateway()

    monkeypatch.setattr(_compat_gateway, "CompatModelGateway", fake_gateway)
    settings = ModelGatewaySettings(
        model="compat-test",
        base_url="https://compat.example.test/v1",
        api_key="provider-key",
        timeout_seconds=12.5,
        retry_attempts=3,
    )

    CompatModelProvider().create(settings)

    assert received == {
        "model": "compat-test",
        "base_url": "https://compat.example.test/v1",
        "api_key": "provider-key",
        "timeout_seconds": 12.5,
        "retry_attempts": 3,
    }

