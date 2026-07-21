"""The pure planner that turns accepted tutoring actions into writes."""

from dataclasses import dataclass, replace
from hashlib import sha256

from mali.actions import (
    Action,
    Actor,
    AskQuestion,
    CertifyPlacement,
    ClearTarget,
    CloseStale,
    FailCheck,
    OverrideMastery,
    PassCheck,
    ProposeTarget,
    RecordAnswer,
    SkipPlacement,
    StartCheck,
    StartPlacement,
)
from mali.checkpoint import Answer, CheckPoint, CheckPointKind, Question
from mali.estimate import PlacementEstimate
from mali.ids import QuestionId, question_id
from mali.plans import ActionPlan, CheckPointWrite, JournalEntry, ProgressWrite
from mali.rules import Refused, evaluate
from mali.snapshot import Snapshot
from mali.templates import canonical_answer


@dataclass(frozen=True, slots=True)
class AvailableMoves:
    """The legal learner-facing choices and next engine action."""

    targets: tuple[tuple[str, tuple[str, ...]], ...]
    can_start_placement: bool
    can_start_check: bool
    engine_action: Action | None


class TutorDesk:
    """Plan guarded learner-record changes from supplied snapshot data."""

    @staticmethod
    def plan(action: Action, snapshot: Snapshot, actor: Actor) -> ActionPlan | Refused:
        """Re-check an action then produce its typed writes or refusal."""
        verdict = evaluate(
            action, snapshot.progress, snapshot.checkpoint, actor, snapshot.policy
        )
        if isinstance(verdict, Refused):
            return verdict
        entry = JournalEntry(action, actor, snapshot.progress.version)
        if isinstance(action, ProposeTarget):
            progress = replace(
                snapshot.progress,
                target=action.skill,
                version=snapshot.progress.version + 1,
            )
            return ActionPlan((ProgressWrite(progress),), entry)
        if isinstance(action, ClearTarget):
            progress = replace(
                snapshot.progress, target=None, version=snapshot.progress.version + 1
            )
            return ActionPlan((ProgressWrite(progress),), entry)
        if isinstance(action, SkipPlacement):
            progress = replace(
                snapshot.progress,
                placed=True,
                version=snapshot.progress.version + 1,
            )
            return ActionPlan((ProgressWrite(progress),), entry)
        if isinstance(action, (StartPlacement, StartCheck)):
            if snapshot.fresh_checkpoint_id is None:
                raise ValueError(
                    "a checkpoint action requires a supplied fresh identifier"
                )
            kind = (
                CheckPointKind.PLACEMENT
                if isinstance(action, StartPlacement)
                else CheckPointKind.CHECK
            )
            checkpoint = CheckPoint(
                snapshot.fresh_checkpoint_id,
                kind,
                snapshot.progress.target if isinstance(action, StartCheck) else None,
                (),
            )
            return ActionPlan((CheckPointWrite(checkpoint),), entry)
        if isinstance(action, PassCheck):
            target = snapshot.progress.target
            if target is None:
                raise AssertionError("accepted pass requires a target")
            skill = next(
                skill
                for skill in snapshot.progress.curriculum.skills
                if skill.code == target
            )
            progress = replace(
                snapshot.progress,
                mask=snapshot.progress.mask | (1 << skill.bit_index),
                target=None,
                version=snapshot.progress.version + 1,
            )
            return ActionPlan((ProgressWrite(progress), CheckPointWrite(None)), entry)
        if isinstance(action, (FailCheck, CloseStale)):
            return ActionPlan((CheckPointWrite(None),), entry)
        if isinstance(action, OverrideMastery):
            skill = next(
                skill
                for skill in snapshot.progress.curriculum.skills
                if skill.code == action.skill
            )
            progress = replace(
                snapshot.progress,
                mask=snapshot.progress.mask | (1 << skill.bit_index),
                target=None
                if snapshot.progress.target == action.skill
                else snapshot.progress.target,
                version=snapshot.progress.version + 1,
            )
            return ActionPlan((ProgressWrite(progress),), entry)
        if isinstance(action, RecordAnswer):
            if snapshot.checkpoint is None:
                raise AssertionError("accepted answer requires a checkpoint")
            normalized = canonical_answer_for(action, snapshot.checkpoint)
            questions = tuple(
                replace(
                    question,
                    answer=Answer(
                        normalized,
                        normalized == question.instance.key,
                    ),
                )
                if question.identifier == action.question
                else question
                for question in snapshot.checkpoint.questions
            )
            checkpoint = replace(snapshot.checkpoint, questions=questions)
            return ActionPlan((CheckPointWrite(checkpoint),), entry)
        if isinstance(action, AskQuestion):
            if snapshot.checkpoint is None:
                raise AssertionError("accepted question requires a checkpoint")
            skill = next(
                item
                for item in snapshot.progress.curriculum.skills
                if item.code == action.skill
            )
            if skill.template is None:
                raise ValueError("an assessable skill requires a question template")
            checkpoint = snapshot.checkpoint
            identifier = _question_identifier(checkpoint, len(checkpoint.questions))
            question = Question(
                identifier,
                action.skill,
                skill.template.instance(action.seed),
            )
            return ActionPlan(
                (
                    CheckPointWrite(
                        replace(checkpoint, questions=(*checkpoint.questions, question))
                    ),
                ),
                entry,
            )
        if isinstance(action, CertifyPlacement):
            checkpoint = snapshot.checkpoint
            questions = checkpoint.questions if checkpoint is not None else ()
            certified = PlacementEstimate.from_answers(
                tuple(questions), snapshot.progress.curriculum, snapshot.policy
            ).certified_mask()
            progress = replace(
                snapshot.progress,
                mask=certified,
                placed=True,
                version=snapshot.progress.version + 1,
            )
            return ActionPlan((ProgressWrite(progress), CheckPointWrite(None)), entry)
        return ActionPlan((), entry)

    @staticmethod
    def available(snapshot: Snapshot) -> AvailableMoves:
        """Enumerate valid study targets and the next engine action."""
        progress = snapshot.progress
        targets = (
            tuple(
                (
                    skill.code,
                    tuple(
                        item.code
                        for item in progress.curriculum.path_to(
                            progress.mask, skill.code
                        )
                    ),
                )
                for skill in progress.curriculum.next_up(progress.mask)
            )
            if progress.placed and snapshot.checkpoint is None
            else ()
        )
        from mali.rules import next_engine_action

        return AvailableMoves(
            targets=targets,
            can_start_placement=not progress.placed and snapshot.checkpoint is None,
            can_start_check=progress.target is not None and snapshot.checkpoint is None,
            engine_action=next_engine_action(snapshot.checkpoint, snapshot.policy),
        )


def canonical_answer_for(action: RecordAnswer, checkpoint: CheckPoint) -> str:
    """Return the already-accepted normalized answer for the named question."""
    question = next(
        item for item in checkpoint.questions if item.identifier == action.question
    )
    normalized = canonical_answer(question.instance.answer_type, action.raw)
    if normalized is None:
        raise AssertionError("accepted answer must be readable")
    return normalized


def _question_identifier(checkpoint: CheckPoint, position: int) -> QuestionId:
    material = f"{checkpoint.identifier}:{position}".encode()
    return question_id(f"q-{sha256(material).hexdigest()[:32]}")
