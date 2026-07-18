"""Product-facing read models derived from verified learner records."""

from dataclasses import dataclass

from mali.curriculum import Curriculum
from mali.plans import ActionPlan
from mali.progress import Progress


@dataclass(frozen=True, slots=True)
class ProgressMap:
    """A learner-friendly map of completed and available skills."""

    mastered: tuple[str, ...]
    next_up: tuple[str, ...]
    later: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """A compact account of one recorded tutoring action."""

    action: str
    actor: str
    version: int


@dataclass(frozen=True, slots=True)
class TeacherSummary:
    """The facts a teacher needs for one learner row."""

    mastered_count: int
    ready_count: int
    current_target: str | None
    evidence_count: int


def progress_map(progress: Progress, curriculum: Curriculum) -> ProgressMap:
    """Build a product-safe progress map from certified learner facts."""
    mastered = tuple(
        skill.title
        for skill in curriculum.skills
        if progress.mask & (1 << skill.bit_index)
    )
    next_up = tuple(skill.title for skill in curriculum.next_up(progress.mask))
    later = tuple(
        skill.title
        for skill in curriculum.skills
        if skill.title not in mastered and skill.title not in next_up
    )
    return ProgressMap(mastered, next_up, later)


def evidence_view(entries: tuple[ActionPlan, ...]) -> tuple[EvidenceView, ...]:
    """Project journal plans into teacher-readable evidence rows."""
    return tuple(
        EvidenceView(
            type(entry.entry.action).__name__,
            entry.entry.actor.value,
            entry.entry.prior_version,
        )
        for entry in entries
    )


def teacher_summary(
    progress: Progress, curriculum: Curriculum, entries: tuple[ActionPlan, ...]
) -> TeacherSummary:
    """Summarize learner progress without making new tutoring decisions."""
    mapped = progress_map(progress, curriculum)
    return TeacherSummary(
        len(mapped.mastered), len(mapped.next_up), progress.target, len(entries)
    )
