"""Explicit normal and degraded modes for model-facing product flows."""

from enum import StrEnum
from os import environ


class DegradationLevel(StrEnum):
    """The model availability level selected for the current process."""

    NORMAL = "l0"
    ITEM_WRITER_FALLBACK = "l1"
    STATIC = "l2"


class DegradationController:
    """Trip model features down safely or pin a level for an outage demonstration."""

    def __init__(self, pinned: DegradationLevel | None = None) -> None:
        self._pinned = pinned
        self._level = DegradationLevel.NORMAL if pinned is None else pinned
        self._writer_fallback_checkpoints: set[str] = set()

    @classmethod
    def from_environment(cls) -> "DegradationController":
        """Read the optional, process-local manual degradation pin."""
        raw = environ.get("MALI_DEGRADATION_LEVEL")
        if raw is None:
            return cls()
        try:
            return cls(DegradationLevel(raw.lower()))
        except ValueError as error:
            choices = ", ".join(level.value for level in DegradationLevel)
            raise ValueError(
                f"MALI_DEGRADATION_LEVEL must be one of: {choices}"
            ) from error

    @property
    def level(self) -> DegradationLevel:
        """Return the effective level, including any manual pin."""
        return self._level

    def use_item_writer(self, checkpoint_id: str | None) -> bool:
        """Return whether one checkpoint may use model-written question prose."""
        if self._level is DegradationLevel.STATIC:
            return False
        if self._pinned is DegradationLevel.ITEM_WRITER_FALLBACK:
            return False
        return checkpoint_id not in self._writer_fallback_checkpoints

    def report_item_writer(
        self,
        checkpoint_id: str | None,
        *,
        used_fallback: bool,
        gateway_failed: bool,
    ) -> None:
        """Fall back for the rest of one checkpoint after writer rejection."""
        if self._pinned is not None:
            return
        if gateway_failed:
            self._level = DegradationLevel.STATIC
        elif used_fallback:
            self._level = DegradationLevel.ITEM_WRITER_FALLBACK
            if checkpoint_id is not None:
                self._writer_fallback_checkpoints.add(checkpoint_id)

    def report_gateway_failure(self) -> None:
        """Trip all model use to static content after a gateway failure."""
        if self._pinned is None:
            self._level = DegradationLevel.STATIC
