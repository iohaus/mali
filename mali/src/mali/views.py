"""Product-facing read models derived from verified learner records."""

from dataclasses import dataclass
from fractions import Fraction

from mali.curriculum import Curriculum, Skill
from mali.ids import SkillCode
from mali.plans import ActionPlan
from mali.progress import Progress
from mali.templates import QuestionInstance, QuestionTemplate


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


@dataclass(frozen=True, slots=True)
class ClosedMistake:
    """A reviewed, incorrect response whose checkpoint has already closed."""

    skill: SkillCode
    question_text: str
    given_answer: str
    correct_answer: str

    def __post_init__(self) -> None:
        for value in (self.question_text, self.given_answer, self.correct_answer):
            if type(value) is not str or not value.strip():
                raise ValueError("closed mistake fields must be non-blank text")


@dataclass(frozen=True, slots=True)
class InstructorMistake:
    """One product-safe prior error supplied to the Instructor flow."""

    question_text: str
    given_answer: str
    correct_answer: str


@dataclass(frozen=True, slots=True)
class LessonExchange:
    """One prior lesson exchange retained so a conversation stays coherent."""

    student_text: str
    tutor_text: str


@dataclass(frozen=True, slots=True)
class InstructorContextPack:
    """The complete safe record projection for one teaching turn."""

    target_title: str
    teaching_card: str
    progress_summary: str
    recent_mistakes: tuple[InstructorMistake, ...]
    student_turn: str
    prerequisite_path: tuple[str, ...]
    recent_conversation: tuple[LessonExchange, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemWriterContextPack:
    """The plain question and its parameter values, never an answer key."""

    question: str
    parameters: tuple[tuple[str, str], ...]


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


def instructor_context(
    progress: Progress,
    recent_mistakes: tuple[ClosedMistake, ...],
    student_turn: str,
    *,
    recent_mistake_limit: int,
    prerequisite_path: tuple[SkillCode, ...] = (),
    recent_conversation: tuple[LessonExchange, ...] = (),
) -> InstructorContextPack:
    """Project progress into model-safe teaching context without checkpoint data."""
    if progress.target is None:
        raise ValueError("an instructor context requires an active target")
    if type(recent_mistake_limit) is not int or recent_mistake_limit < 1:
        raise ValueError("recent mistake limit must be a positive integer")
    if type(student_turn) is not str:
        raise ValueError("student turn must be text")
    if any(type(exchange) is not LessonExchange for exchange in recent_conversation):
        raise ValueError("recent conversation must hold lesson exchanges")
    target = _skill(progress.curriculum, progress.target)
    matching_mistakes = tuple(
        InstructorMistake(
            mistake.question_text,
            mistake.given_answer,
            mistake.correct_answer,
        )
        for mistake in recent_mistakes
        if mistake.skill == target.code
    )[-recent_mistake_limit:]
    mastered = tuple(
        skill.title
        for skill in progress.curriculum.skills
        if progress.mask & (1 << skill.bit_index)
    )
    mastered_text = ", ".join(mastered) if mastered else "nothing yet"
    return InstructorContextPack(
        target.title,
        target.card,
        f"mastered: {mastered_text}; working on: {target.title}",
        matching_mistakes,
        student_turn,
        tuple(_skill(progress.curriculum, code).title for code in prerequisite_path),
        recent_conversation,
    )


def item_writer_context(
    template: QuestionTemplate, instance: QuestionInstance
) -> ItemWriterContextPack:
    """Return only authored parameter values, never a computed answer key."""
    values = dict(instance.values)
    parameters: list[tuple[str, str]] = []
    for parameter in template.parameters:
        value = values.get(parameter.name)
        if not isinstance(value, Fraction):
            raise ValueError("question instance is missing a template parameter")
        parameters.append((parameter.name, str(value)))
    return ItemWriterContextPack(instance.text, tuple(parameters))


def _skill(curriculum: Curriculum, code: SkillCode) -> Skill:
    try:
        return next(skill for skill in curriculum.skills if skill.code == code)
    except StopIteration as error:
        raise ValueError("context references an unknown skill") from error
