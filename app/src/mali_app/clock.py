"""Time sources used by the application boundary."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Supply an event time for records written by the application."""

    def now(self) -> datetime: ...


class SystemClock:
    """Use the host's UTC clock for durable record timestamps."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""
        return datetime.now(UTC)


def as_storage_time(value: datetime) -> str:
    """Render one aware timestamp in SQLite's stable text form."""
    if value.tzinfo is None:
        raise ValueError("record timestamps must include a timezone")
    return value.astimezone(UTC).isoformat()
