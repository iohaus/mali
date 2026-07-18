"""Immutable input data for one tutoring decision."""

from dataclasses import dataclass

from mali.checkpoint import CheckPoint
from mali.ids import CheckPointId
from mali.policy import TutorPolicy
from mali.progress import Progress


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The complete learner data supplied to a pure planning call."""

    progress: Progress
    checkpoint: CheckPoint | None
    policy: TutorPolicy
    fresh_checkpoint_id: CheckPointId | None
