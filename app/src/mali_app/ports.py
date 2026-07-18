"""Typed application-boundary ports."""

from typing import Protocol

from mali.actions import Action, Actor
from mali.ids import LearnerId, SkillCode
from mali.snapshot import Snapshot
from mali.views import ClosedMistake

from mali_app.store_types import AuditResult, ExecutionResult, TeachingTrace


class RecordStore(Protocol):
    """Read learner snapshots, apply requested actions, and audit history."""

    def snapshot(self, learner: LearnerId) -> Snapshot: ...

    def execute(
        self,
        learner: LearnerId,
        action: Action,
        actor: Actor,
        *,
        expected_version: int | None = None,
    ) -> ExecutionResult: ...

    def audit(self, learner: LearnerId) -> AuditResult: ...

    def recent_mistakes(
        self, learner: LearnerId, skill: SkillCode, limit: int
    ) -> tuple[ClosedMistake, ...]: ...

    def record_teaching_trace(self, trace: TeachingTrace) -> None: ...
