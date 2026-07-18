"""Validated, bounded model rendering for deterministic question instances."""

from dataclasses import dataclass

from mali.policy import TutorPolicy
from mali.templates import QuestionInstance, QuestionTemplate, validate_rendering
from mali.views import item_writer_context
from pydantic import BaseModel, ConfigDict

from mali_app.model_gateway import (
    GatewayError,
    GatewayTimeout,
    GatewayUnavailable,
    ModelGateway,
    StructuredRequest,
)
from mali_app.prompt_assets import item_writer_prompt, render_item_writer_context


class RenderingRejected(Exception):
    """Raised when otherwise parsed question prose fails core validation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ItemWriterResponse(BaseModel):
    """The only structured value accepted from the Item Writer model call."""

    model_config = ConfigDict(extra="forbid")

    question_text: str


@dataclass(frozen=True, slots=True)
class ItemWriterResult:
    """A displayed question and the bounded rendering path that produced it."""

    question_text: str
    attempts: int
    used_fallback: bool
    gateway_failed: bool
    rejection_reasons: tuple[str, ...]


class ItemWriter:
    """Render engaging question prose without any grading authority."""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    def render(
        self,
        policy: TutorPolicy,
        template: QuestionTemplate,
        instance: QuestionInstance,
    ) -> ItemWriterResult:
        """Try bounded model renderings, then use plain deterministic text."""
        instructions = item_writer_prompt(policy).instructions
        input_text = render_item_writer_context(item_writer_context(template, instance))
        rejection_reasons: list[str] = []
        gateway_failed = False
        for attempt in range(1, policy.flow_budget.item_writer_retries + 2):
            request = StructuredRequest(
                instructions,
                input_text,
                policy.flow_budget.max_output_tokens,
                ItemWriterResponse,
            )
            try:
                response = self._gateway.structured(request)
                _accept_rendering(instance, response.question_text)
            except GatewayError as error:
                reason = type(error).__name__
                gateway_failed = gateway_failed or isinstance(
                    error, (GatewayTimeout, GatewayUnavailable)
                )
            except RenderingRejected as error:
                reason = error.reason
            else:
                return ItemWriterResult(
                    response.question_text,
                    attempt,
                    False,
                    gateway_failed,
                    tuple(rejection_reasons),
                )
            rejection_reasons.append(reason)
            input_text = _retry_input(input_text, reason)
        return ItemWriterResult(
            instance.text,
            policy.flow_budget.item_writer_retries + 1,
            True,
            gateway_failed,
            tuple(rejection_reasons),
        )


def _accept_rendering(instance: QuestionInstance, text: str) -> None:
    verdict = validate_rendering(instance, text)
    if not verdict.accepted:
        raise RenderingRejected(verdict.reason or "question rendering was rejected")


def _retry_input(input_text: str, reason: str) -> str:
    return (
        f"{input_text}\n"
        "<previous-rendering-feedback>\n"
        f"The previous rendering was rejected: {reason}. Try again using every "
        "parameter exactly as written.\n"
        "</previous-rendering-feedback>"
    )
