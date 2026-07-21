"""Pure acceptance rules for tutoring intents."""

from dataclasses import dataclass
from enum import StrEnum

from mali.actions import (
    Action,
    Actor,
    AdoptPolicy,
    AskQuestion,
    CertifyPlacement,
    ClearTarget,
    CloseStale,
    FailCheck,
    LoadCurriculum,
    OverrideMastery,
    PassCheck,
    ProposeTarget,
    RecordAnswer,
    RegisterLearner,
    RemoveLearner,
    SkipPlacement,
    StartCheck,
    StartPlacement,
    TeachEpisode,
)
from mali.checkpoint import CheckPoint, CheckPointKind, cannot_pass, has_passed
from mali.policy import TutorPolicy
from mali.progress import Progress
from mali.templates import canonical_answer


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
    QUESTION_NOT_FOUND = "question_not_found"
    QUESTION_ALREADY_ANSWERED = "question_already_answered"
    ANSWER_NOT_READABLE = "answer_not_readable"
    PLACEMENT_NOT_READY = "placement_not_ready"
    CHECK_NOT_DECIDED = "check_not_decided"


@dataclass(frozen=True, slots=True)
class Allowed:
    """An intent whose requirements currently hold."""


@dataclass(frozen=True, slots=True)
class Refused:
    """An intent refused with a stable product-safe reason."""

    reason: RefusalReason


RuleVerdict = Allowed | Refused


def next_engine_action(
    checkpoint: CheckPoint | None, policy: TutorPolicy
) -> CertifyPlacement | PassCheck | FailCheck | None:
    """Select the deterministic terminal action for an open mastery check."""
    if checkpoint is None:
        return None
    if checkpoint.kind is CheckPointKind.PLACEMENT:
        if len(checkpoint.questions) >= policy.question_budget and all(
            question.answer is not None for question in checkpoint.questions
        ):
            return CertifyPlacement()
        return None
    if has_passed(checkpoint.questions, policy.pass_rule.needed):
        return PassCheck()
    if cannot_pass(
        checkpoint.questions, policy.pass_rule.needed, policy.pass_rule.asked
    ):
        return FailCheck()
    return None


def evaluate(
    action: Action,
    progress: Progress,
    checkpoint: CheckPoint | None,
    actor: Actor,
    policy: TutorPolicy,
) -> RuleVerdict:
    """Evaluate any typed action against the current learner data."""
    match action:
        case StartPlacement():
            return start_placement(progress, checkpoint, actor)
        case SkipPlacement():
            return skip_placement(progress, checkpoint, actor)
        case ProposeTarget():
            return propose_target(progress, checkpoint, action, actor)
        case TeachEpisode():
            return teach_episode(progress, action, actor)
        case StartCheck():
            return start_check(progress, checkpoint, actor)
        case OverrideMastery():
            return override_mastery(progress, checkpoint, action, actor)
        case AskQuestion():
            return _ask_question(checkpoint, actor, action, policy)
        case RecordAnswer():
            return _record_answer(checkpoint, actor, action)
        case CertifyPlacement():
            return _certify_placement(checkpoint, actor, policy)
        case PassCheck() | FailCheck():
            return _terminal_check(checkpoint, actor, action, policy)
        case ClearTarget():
            return _clear_target(progress, checkpoint, actor)
        case CloseStale():
            return _checkpoint_only(checkpoint, actor)
        case RegisterLearner() | RemoveLearner() | LoadCurriculum() | AdoptPolicy():
            return (
                Allowed()
                if actor is Actor.ADMIN
                else Refused(RefusalReason.INVALID_ACTOR)
            )


def _ask_question(
    checkpoint: CheckPoint | None,
    actor: Actor,
    action: AskQuestion,
    policy: TutorPolicy,
) -> RuleVerdict:
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is None:
        return Refused(RefusalReason.NOTHING_TO_CHECK)
    if len(checkpoint.questions) >= policy.question_budget:
        return Refused(RefusalReason.CHECK_NOT_DECIDED)
    if checkpoint.kind is CheckPointKind.CHECK and checkpoint.target != action.skill:
        return Refused(RefusalReason.NOT_CURRENT_TARGET)
    return Allowed()


def _record_answer(
    checkpoint: CheckPoint | None, actor: Actor, action: RecordAnswer
) -> RuleVerdict:
    if actor is not Actor.STUDENT:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is None:
        return Refused(RefusalReason.NOTHING_TO_CHECK)
    question = next(
        (item for item in checkpoint.questions if item.identifier == action.question),
        None,
    )
    if question is None:
        return Refused(RefusalReason.QUESTION_NOT_FOUND)
    if question.answer is not None:
        return Refused(RefusalReason.QUESTION_ALREADY_ANSWERED)
    if canonical_answer(question.instance.answer_type, action.raw) is None:
        return Refused(RefusalReason.ANSWER_NOT_READABLE)
    return Allowed()


def _certify_placement(
    checkpoint: CheckPoint | None, actor: Actor, policy: TutorPolicy
) -> RuleVerdict:
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is None or checkpoint.kind is not CheckPointKind.PLACEMENT:
        return Refused(RefusalReason.PLACEMENT_NOT_READY)
    if len(checkpoint.questions) < policy.question_budget and any(
        question.answer is None for question in checkpoint.questions
    ):
        return Refused(RefusalReason.PLACEMENT_NOT_READY)
    return Allowed()


def _terminal_check(
    checkpoint: CheckPoint | None,
    actor: Actor,
    action: PassCheck | FailCheck,
    policy: TutorPolicy,
) -> RuleVerdict:
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    selected = next_engine_action(checkpoint, policy)
    if selected is None or type(selected) is not type(action):
        return Refused(RefusalReason.CHECK_NOT_DECIDED)
    return Allowed()


def _clear_target(
    progress: Progress, checkpoint: CheckPoint | None, actor: Actor
) -> RuleVerdict:
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    if checkpoint is not None:
        return Refused(RefusalReason.CHECK_IN_PROGRESS)
    if progress.target is None:
        return Refused(RefusalReason.NOTHING_TO_CHECK)
    return Allowed()


def _checkpoint_only(checkpoint: CheckPoint | None, actor: Actor) -> RuleVerdict:
    if actor is not Actor.ENGINE:
        return Refused(RefusalReason.INVALID_ACTOR)
    return (
        Allowed() if checkpoint is not None else Refused(RefusalReason.NOTHING_TO_CHECK)
    )


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


def skip_placement(
    progress: Progress, checkpoint: CheckPoint | None, actor: Actor
) -> RuleVerdict:
    """Check whether a learner can decline placement and start at zero.

    Skipping resolves the starting point without crediting any skill; only
    checked answers ever move certified progress.
    """
    if actor is not Actor.STUDENT:
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
