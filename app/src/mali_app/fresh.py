"""Fresh identifiers and deterministic values for application records."""

from secrets import randbits
from typing import Protocol
from uuid import uuid4

from mali.ids import CheckPointId, checkpoint_id


class FreshSource(Protocol):
    """Supply ids and question values outside the deterministic core."""

    def checkpoint_id(self) -> CheckPointId: ...

    def journal_id(self) -> str: ...

    def question_seed(self) -> int: ...


class SystemFreshSource:
    """Use opaque system-generated values for new durable records."""

    def checkpoint_id(self) -> CheckPointId:
        """Create a core-validated checkpoint identifier."""
        return checkpoint_id(f"c-{uuid4().hex}")

    def journal_id(self) -> str:
        """Create a unique identifier for one journal entry."""
        return f"j-{uuid4().hex}"

    def question_seed(self) -> int:
        """Create a non-negative seed for one question instance."""
        return randbits(63)


class CountingFreshSource:
    """Produce repeatable values for automated checks and the demo seed."""

    def __init__(self) -> None:
        self._counter = 0

    def checkpoint_id(self) -> CheckPointId:
        """Create the next repeatable checkpoint identifier."""
        return checkpoint_id(f"checkpoint-{self._next()}")

    def journal_id(self) -> str:
        """Create the next repeatable journal identifier."""
        return f"journal-{self._next()}"

    def question_seed(self) -> int:
        """Create the next repeatable question seed."""
        return self._next()

    def _next(self) -> int:
        self._counter += 1
        return self._counter
