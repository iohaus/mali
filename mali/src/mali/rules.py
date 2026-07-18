"""Pure acceptance rules for tutoring intents."""

from dataclasses import dataclass
from enum import StrEnum

from mali.actions import (
    Actor,
    OverrideMastery,
    ProposeTarget,
    TeachEpisode,
)
from mali.checkpoint import CheckPoint
from mali.progress import Progress


class RefusalReason(StrEnum):
    """Product-safe reasons an intent cannot proceed."""

    NOT_READY_YET = "not_ready_yet"
    ALREADY_MASTERED = "already_mastered"
    CHECK_IN_PROGRESS = "check_in_progress"
    PLACEMENT_ALREADY_DONE = "placement_already_done"
    PLACEMENT_REQUIRED = "placement_required"
    NOTHING_TO_CHECK = "nothing_to_check"
    NOT_CURRENT_TARGET = "not_current_target"
    TEACHER_REQUIRED = "teacher_required"
    INVALID_ACTOR = "invalid_actor"


@dataclass(frozen=True, slots=True)
class Allowed:
    """An intent whose requirements currently hold."""


@dataclass(frozen=True, slots=True)
class Refused:
    """An intent refused with a stable product-safe reason."""

    reason: RefusalReason


RuleVerdict = Allowed | Refused


def start_placement(
    progress: Progress, checkpoint: CheckPoint | None, actor: Actor
) -> RuleVerdict:
    """Check whether a learner can start their one placement session."""
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is not None:
        return Refused(RefusalReason.CHECK_IN_PROGRESS)
    if progress.placed:
        return Refused(RefusalReason.PLACEMENT_ALREADY_DONE)
    return Allowed()


def propose_target(
    progress: Progress,
    checkpoint: CheckPoint | None,
    action: ProposeTarget,
    actor: Actor,
) -> RuleVerdict:
    """Check whether a requested skill is ready for study."""
    if actor not in (Actor.STUDENT, Actor.INSTRUCTOR):
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is not None:
        return Refused(RefusalReason.CHECK_IN_PROGRESS)
    if not progress.placed:
        return Refused(RefusalReason.PLACEMENT_REQUIRED)
    if action.skill in {
        skill.code for skill in progress.curriculum.review_set(progress.mask)
    }:
        return Refused(RefusalReason.ALREADY_MASTERED)
    if action.skill not in {
        skill.code for skill in progress.curriculum.next_up(progress.mask)
    }:
        return Refused(RefusalReason.NOT_READY_YET)
    return Allowed()


def start_check(
    progress: Progress, checkpoint: CheckPoint | None, actor: Actor
) -> RuleVerdict:
    """Check whether the learner has a target ready for assessment."""
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is not None:
        return Refused(RefusalReason.CHECK_IN_PROGRESS)
    if progress.target is None:
        return Refused(RefusalReason.NOTHING_TO_CHECK)
    return Allowed()


def teach_episode(
    progress: Progress, action: TeachEpisode, actor: Actor
) -> RuleVerdict:
    """Check that an instructor teaches only the active target."""
    if actor is not Actor.INSTRUCTOR:
        return Refused(RefusalReason.INVALID_ACTOR)
    if progress.target != action.skill:
        return Refused(RefusalReason.NOT_CURRENT_TARGET)
    return Allowed()


def override_mastery(
    progress: Progress,
    checkpoint: CheckPoint | None,
    action: OverrideMastery,
    actor: Actor,
) -> RuleVerdict:
    """Check teacher authority and prerequisite-safe mastery override."""
    if actor is not Actor.TEACHER:
        return Refused(RefusalReason.TEACHER_REQUIRED)
    if checkpoint is not None:
        return Refused(RefusalReason.CHECK_IN_PROGRESS)
    if not progress.placed:
        return Refused(RefusalReason.PLACEMENT_REQUIRED)
    bit = next(
        (
            1 << skill.bit_index
            for skill in progress.curriculum.skills
            if skill.code == action.skill
        ),
        None,
    )
    if bit is None or not progress.curriculum.is_reachable(progress.mask | bit):
        return Refused(RefusalReason.NOT_READY_YET)
    if progress.mask & bit:
        return Refused(RefusalReason.ALREADY_MASTERED)
    return Allowed()
