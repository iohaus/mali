"""Bounded streamed Instructor episodes with guarded record requests."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from json import JSONDecodeError, dumps, loads
from typing import cast
from uuid import uuid4

from mali.actions import Actor, ProposeTarget, StartCheck
from mali.curriculum import Skill
from mali.errors import InvalidIdentifier
from mali.ids import LearnerId, SkillCode, skill_code
from mali.rules import RefusalReason
from mali.snapshot import Snapshot
from mali.views import InstructorContextPack, instructor_context

from mali_app.degradation import DegradationController, DegradationLevel
from mali_app.model_gateway import (
    FunctionTool,
    GatewayError,
    ModelGateway,
    StreamRequest,
)
from mali_app.ports import RecordStore
from mali_app.prompt_assets import instructor_prompt, render_instructor_context
from mali_app.store_types import ExecutionStatus, TeachingTrace

_LOG = logging.getLogger(__name__)


class InstructorOutcome(StrEnum):
    """The typed terminal state of one teaching episode."""

    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    GATEWAY_FAILED = "gateway_failed"
    STUDENT_LEFT = "student_left"


@dataclass(frozen=True, slots=True)
class InstructorEvent:
    """One student-visible SSE payload emitted by an Instructor episode."""

    text: str | None = None
    outcome: InstructorOutcome | None = None


def instructor_tools() -> tuple[FunctionTool, ...]:
    """Return the complete, closed JSON-schema function surface."""
    empty: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    skill: dict[str, object] = {
        "type": "object",
        "properties": {"skill_code": {"type": "string"}},
        "required": ["skill_code"],
        "additionalProperties": False,
    }
    return (
        FunctionTool(
            "get_progress_summary", "Read the current progress summary.", empty
        ),
        FunctionTool("get_teaching_card", "Read one skill's teaching card.", skill),
        FunctionTool("get_recent_mistakes", "Read recent closed mistakes.", empty),
        FunctionTool("get_path_to", "Read the preparation path to one skill.", skill),
        FunctionTool(
            "propose_target", "Request a study target through the desk.", skill
        ),
        FunctionTool("request_check", "Request a check through the desk.", empty),
    )


class InstructorEpisode:
    """Execute a stateless, policy-bounded teaching episode for one learner."""

    def __init__(
        self,
        store: RecordStore,
        gateway: ModelGateway | None,
        degradation: DegradationController,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._degradation = degradation

    def stream(
        self,
        learner: LearnerId,
        snapshot: Snapshot,
        student_turn: str,
        *,
        prerequisite_path: tuple[SkillCode, ...] = (),
    ) -> Iterator[InstructorEvent]:
        """Yield text promptly and persist every completed provider turn."""
        context = _context_for(
            self._store,
            learner,
            snapshot,
            student_turn,
            prerequisite_path=prerequisite_path,
        )
        episode_id = f"episode-{uuid4().hex}"
        gateway = self._gateway
        _LOG.info(
            "instructor episode started learner=%s episode=%s "
            "target=%s level=%s gateway=%s",
            learner,
            episode_id,
            snapshot.progress.target,
            self._degradation.level.value,
            gateway is not None,
        )
        if self._degradation.level is DegradationLevel.STATIC or gateway is None:
            text = _static_lesson(context)
            self._save_trace(
                learner,
                snapshot,
                episode_id,
                "static",
                text,
                0,
                0,
                InstructorOutcome.COMPLETED,
            )
            _LOG.info(
                "instructor episode completed learner=%s episode=%s mode=static",
                learner,
                episode_id,
            )
            yield InstructorEvent(text=text)
            yield InstructorEvent(outcome=InstructorOutcome.COMPLETED)
            return

        policy = snapshot.policy
        instructions = instructor_prompt(policy).instructions
        base_input = render_instructor_context(context)
        input_text = base_input
        tools = _InstructorTools(self._store, learner, snapshot, context)
        turns = 0
        reserved_tokens = 0
        while True:
            remaining_tokens = policy.flow_budget.max_episode_tokens - reserved_tokens
            if turns >= policy.flow_budget.max_turns or remaining_tokens < 1:
                text = "Let's pick this up next time."
                self._save_trace(
                    learner,
                    snapshot,
                    episode_id,
                    "static",
                    text,
                    0,
                    0,
                    InstructorOutcome.BUDGET_EXHAUSTED,
                )
                _LOG.info(
                    "instructor episode budget exhausted learner=%s episode=%s "
                    "turns=%s reserved_tokens=%s",
                    learner,
                    episode_id,
                    turns,
                    reserved_tokens,
                )
                yield InstructorEvent(text=text)
                yield InstructorEvent(outcome=InstructorOutcome.BUDGET_EXHAUSTED)
                return
            output_limit = min(policy.flow_budget.max_output_tokens, remaining_tokens)
            request = StreamRequest(
                instructions,
                input_text,
                output_limit,
                instructor_tools(),
            )
            transcript: list[str] = []
            tool_results: list[dict[str, object]] = []
            _LOG.debug(
                "instructor model turn learner=%s episode=%s turn=%s output_limit=%s",
                learner,
                episode_id,
                turns + 1,
                output_limit,
            )
            try:
                for delta in gateway.stream(request):
                    if delta.tool_name is not None:
                        result = tools.invoke(delta.tool_name, delta.tool_arguments)
                        tool_results.append(result)
                        _LOG.debug(
                            "instructor function completed learner=%s episode=%s "
                            "function=%s ok=%s reason=%s",
                            learner,
                            episode_id,
                            delta.tool_name,
                            result.get("ok"),
                            result.get("reason"),
                        )
                    elif delta.text:
                        transcript.append(delta.text)
                        yield InstructorEvent(text=delta.text)
            except GatewayError:
                self._degradation.report_gateway_failure()
                text = _static_lesson(context)
                self._save_trace(
                    learner,
                    snapshot,
                    episode_id,
                    gateway.identity.trace_label,
                    "".join(transcript) + text,
                    0,
                    output_limit,
                    InstructorOutcome.GATEWAY_FAILED,
                )
                _LOG.warning(
                    "instructor gateway failed learner=%s episode=%s; "
                    "switched level=%s",
                    learner,
                    episode_id,
                    self._degradation.level.value,
                )
                yield InstructorEvent(text=text)
                yield InstructorEvent(outcome=InstructorOutcome.GATEWAY_FAILED)
                return

            turns += 1
            reserved_tokens += output_limit
            terminal = not tool_results
            self._save_trace(
                learner,
                snapshot,
                episode_id,
                gateway.identity.trace_label,
                "".join(transcript),
                0,
                output_limit,
                InstructorOutcome.COMPLETED if terminal else "continued",
            )
            if terminal:
                _LOG.info(
                    "instructor episode completed learner=%s episode=%s "
                    "turns=%s reserved_tokens=%s",
                    learner,
                    episode_id,
                    turns,
                    reserved_tokens,
                )
                yield InstructorEvent(outcome=InstructorOutcome.COMPLETED)
                return
            input_text = _tool_results_input(base_input, tool_results)

    def _save_trace(
        self,
        learner: LearnerId,
        snapshot: Snapshot,
        episode_id: str,
        model: str,
        transcript: str,
        tokens_in: int,
        tokens_out: int,
        outcome: InstructorOutcome | str,
    ) -> None:
        target = snapshot.progress.target
        if target is None:
            raise ValueError("a teaching trace requires an active target")
        self._store.record_teaching_trace(
            TeachingTrace(
                learner,
                target,
                episode_id,
                model,
                snapshot.policy.instructor_prompt_version,
                snapshot.policy.version,
                transcript,
                tokens_in,
                tokens_out,
                outcome.value if isinstance(outcome, InstructorOutcome) else outcome,
            )
        )


class _InstructorTools:
    """Run the closed tool surface against an episode's initial read projection."""

    def __init__(
        self,
        store: RecordStore,
        learner: LearnerId,
        snapshot: Snapshot,
        context: InstructorContextPack,
    ) -> None:
        self._store = store
        self._learner = learner
        self._snapshot = snapshot
        self._context = context
        self._requests = 0

    def invoke(self, name: str, raw_arguments: str | None) -> dict[str, object]:
        """Return typed data or a refusal; never raise model-controlled input."""
        arguments = _arguments(raw_arguments)
        if arguments is None:
            return {"ok": False, "reason": "invalid_arguments"}
        if name in {"propose_target", "request_check"}:
            if self._requests >= self._snapshot.policy.flow_budget.max_requests:
                return {"ok": False, "reason": "budget_exhausted"}
            self._requests += 1
        if name == "get_progress_summary":
            return {"ok": True, "summary": self._context.progress_summary}
        if name == "get_teaching_card":
            skill = self._skill_argument(arguments)
            if skill is None:
                return {"ok": False, "reason": "not_found"}
            return {"ok": True, "title": skill.title, "card": skill.card}
        if name == "get_recent_mistakes":
            return {
                "ok": True,
                "mistakes": [
                    {
                        "question": mistake.question_text,
                        "given": mistake.given_answer,
                        "correct": mistake.correct_answer,
                    }
                    for mistake in self._context.recent_mistakes
                ],
            }
        if name == "get_path_to":
            skill = self._skill_argument(arguments)
            if skill is None:
                return {"ok": False, "reason": "not_found"}
            path = self._snapshot.progress.curriculum.path_to(
                self._snapshot.progress.mask, skill.code
            )
            return {"ok": True, "path": [item.title for item in path]}
        if name == "propose_target":
            return self._propose_target(arguments)
        if name == "request_check":
            return self._request_check()
        return {"ok": False, "reason": "unknown_function"}

    def _propose_target(self, arguments: dict[str, object]) -> dict[str, object]:
        skill = self._skill_argument(arguments)
        if skill is None:
            return {"ok": False, "reason": "not_found"}
        result = self._store.execute(
            self._learner,
            ProposeTarget(skill.code),
            Actor.INSTRUCTOR,
            expected_version=self._snapshot.progress.version,
        )
        if result.status is ExecutionStatus.COMMITTED and result.snapshot is not None:
            self._snapshot = result.snapshot
            return {"ok": True, "target": skill.title}
        if result.status is ExecutionStatus.REFUSED and result.refusal is not None:
            refusal: dict[str, object] = {
                "ok": False,
                "reason": result.refusal.value,
            }
            if result.refusal is RefusalReason.NOT_READY_YET:
                refusal["path_to"] = [
                    item.title
                    for item in self._snapshot.progress.curriculum.path_to(
                        self._snapshot.progress.mask, skill.code
                    )
                ]
            return refusal
        return {"ok": False, "reason": "stale_record"}

    def _request_check(self) -> dict[str, object]:
        result = self._store.execute(
            self._learner,
            StartCheck(),
            Actor.ENGINE,
            expected_version=self._snapshot.progress.version,
        )
        if result.status is ExecutionStatus.COMMITTED and result.snapshot is not None:
            self._snapshot = result.snapshot
            return {"ok": True, "check": "started"}
        if result.status is ExecutionStatus.REFUSED and result.refusal is not None:
            return {"ok": False, "reason": result.refusal.value}
        return {"ok": False, "reason": "stale_record"}

    def _skill_argument(self, arguments: dict[str, object]) -> Skill | None:
        try:
            code = skill_code(arguments.get("skill_code"))
        except InvalidIdentifier:
            return None
        return next(
            (
                skill
                for skill in self._snapshot.progress.curriculum.skills
                if skill.code == code
            ),
            None,
        )


def _context_for(
    store: RecordStore,
    learner: LearnerId,
    snapshot: Snapshot,
    student_turn: str,
    *,
    prerequisite_path: tuple[SkillCode, ...] = (),
) -> InstructorContextPack:
    target = snapshot.progress.target
    if target is None:
        raise ValueError("an instructor episode requires an active target")
    return instructor_context(
        snapshot.progress,
        store.recent_mistakes(
            learner, target, snapshot.policy.flow_budget.recent_mistake_limit
        ),
        student_turn,
        recent_mistake_limit=snapshot.policy.flow_budget.recent_mistake_limit,
        prerequisite_path=prerequisite_path,
    )


def _arguments(raw_arguments: str | None) -> dict[str, object] | None:
    if raw_arguments is None:
        return None
    try:
        parsed = cast(object, loads(raw_arguments))
    except JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    values = cast(dict[object, object], parsed)
    if any(not isinstance(key, str) for key in values):
        return None
    return cast(dict[str, object], values)


def _tool_results_input(base_input: str, results: list[dict[str, object]]) -> str:
    return (
        f"{base_input}\n"
        "<function-results>\n"
        f"{dumps(results, sort_keys=True)}\n"
        "</function-results>"
    )


def _static_lesson(context: InstructorContextPack) -> str:
    route = (
        f"\n\nYour path\nStart with {', then '.join(context.prerequisite_path)}."
        if context.prerequisite_path
        else ""
    )
    return (
        f"{context.target_title}\n\n"
        f"What to focus on\n{context.teaching_card}\n\n"
        "Next step\nTake one small step at a time, then explain it in your own words."
        f"{route}"
    )
