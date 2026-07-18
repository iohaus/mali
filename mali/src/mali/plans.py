"""Typed state changes produced by an accepted tutoring action."""

from dataclasses import dataclass

from mali.actions import Action, Actor
from mali.checkpoint import CheckPoint
from mali.progress import Progress


@dataclass(frozen=True, slots=True)
class ProgressWrite:
    """A complete replacement for one learner progress record."""

    progress: Progress


@dataclass(frozen=True, slots=True)
class CheckPointWrite:
    """A complete replacement for one learner checkpoint record."""

    checkpoint: CheckPoint | None


type StateWrite = ProgressWrite | CheckPointWrite


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """The evidence envelope accompanying a planned state change."""

    action: Action
    actor: Actor
    prior_version: int


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """A deterministic set of writes that the shell may commit atomically."""

    writes: tuple[StateWrite, ...]
    entry: JournalEntry
