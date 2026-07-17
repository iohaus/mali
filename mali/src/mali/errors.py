"""Typed failures raised when invalid tutoring data enters the core."""


class MaliDomainError(ValueError):
    """Base class for invalid domain data."""


class InvalidIdentifier(MaliDomainError):
    """Raised when an identifier is not a valid opaque value."""


class InvalidSkill(MaliDomainError):
    """Raised when a skill cannot be represented safely."""
