"""Replay verification for immutable tutoring action plans."""

from mali.errors import JournalCorruption
from mali.plans import ActionPlan, ProgressWrite
from mali.progress import Progress


class Journal:
    """Fold action plans and reject any broken learner-version chain."""

    @staticmethod
    def replay(initial: Progress, entries: tuple[ActionPlan, ...]) -> Progress:
        """Reproduce progress from journaled plans or raise corruption."""
        current = initial
        for position, entry in enumerate(entries):
            if entry.entry.prior_version != current.version:
                raise JournalCorruption(
                    f"entry {position} has an invalid prior version"
                )
            writes = tuple(
                write for write in entry.writes if isinstance(write, ProgressWrite)
            )
            if len(writes) > 1:
                raise JournalCorruption(
                    f"entry {position} has multiple progress writes"
                )
            if not writes:
                continue
            next_progress = writes[0].progress
            if next_progress.learner != current.learner:
                raise JournalCorruption(f"entry {position} changes learner identity")
            if next_progress.curriculum_version != current.curriculum_version:
                raise JournalCorruption(f"entry {position} changes curriculum version")
            if next_progress.version != current.version + 1:
                raise JournalCorruption(f"entry {position} has an invalid next version")
            current = next_progress
        return current
