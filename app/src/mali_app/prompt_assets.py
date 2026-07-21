"""Load versioned prompts and render the safe context supplied to model flows."""

from dataclasses import dataclass
from importlib.resources import files

from mali.policy import TutorPolicy
from mali.views import InstructorContextPack, ItemWriterContextPack


class PromptAssetError(ValueError):
    """Raised when a policy selects a prompt asset that is not bundled."""


@dataclass(frozen=True, slots=True)
class PromptAsset:
    """One immutable, named system-prompt asset."""

    version: str
    instructions: str


def instructor_prompt(policy: TutorPolicy) -> PromptAsset:
    """Load the Instructor asset selected by the active policy."""
    return _load_prompt(policy.instructor_prompt_version, "instructor")


def item_writer_prompt(policy: TutorPolicy) -> PromptAsset:
    """Load the Item Writer asset selected by the active policy."""
    return _load_prompt(policy.item_writer_prompt_version, "item_writer")


def _rendered_conversation(context: InstructorContextPack) -> str:
    if not context.recent_conversation:
        return "none"
    return "\n".join(
        f"student: {exchange.student_text}\ntutor: {exchange.tutor_text}"
        for exchange in context.recent_conversation
    )


def render_instructor_context(context: InstructorContextPack) -> str:
    """Delimit record and student data as input, never system instructions."""
    mistakes = "\n".join(
        "- question: "
        f"{mistake.question_text}\n"
        f"  given: {mistake.given_answer}\n"
        f"  correct: {mistake.correct_answer}"
        for mistake in context.recent_mistakes
    )
    path = ", ".join(context.prerequisite_path) or "none"
    return (
        "<teaching-context>\n"
        f"target: {context.target_title}\n"
        f"teaching-card: {context.teaching_card}\n"
        f"progress: {context.progress_summary}\n"
        f"prerequisite-path: {path}\n"
        "</teaching-context>\n"
        "<recorded-mistakes>\n"
        f"{mistakes or 'none'}\n"
        "</recorded-mistakes>\n"
        "<recent-conversation>\n"
        f"{_rendered_conversation(context)}\n"
        "</recent-conversation>\n"
        "<untrusted-student-turn>\n"
        f"{context.student_turn}\n"
        "</untrusted-student-turn>"
    )


def render_item_writer_context(context: ItemWriterContextPack) -> str:
    """Render the question-and-values Item Writer input without a key."""
    parameters = "\n".join(f"- {name}: {value}" for name, value in context.parameters)
    return (
        f"<question>\n{context.question}\n</question>\n"
        f"<question-parameters>\n{parameters}\n</question-parameters>"
    )


def _load_prompt(version: str, flow: str) -> PromptAsset:
    expected_prefix = f"{flow}_v"
    if not version.startswith(expected_prefix):
        raise PromptAssetError(f"{flow} policy selected the wrong prompt family")
    asset = files("mali_app.prompts").joinpath(f"{version}.md")
    if not asset.is_file():
        raise PromptAssetError(f"prompt asset {version!r} is not bundled")
    instructions = asset.read_text(encoding="utf-8").strip()
    if not instructions:
        raise PromptAssetError(f"prompt asset {version!r} is blank")
    return PromptAsset(version, instructions)
