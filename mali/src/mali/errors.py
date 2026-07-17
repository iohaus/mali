"""Typed failures raised when invalid tutoring data enters the core."""


class MaliDomainError(ValueError):
    """Base class for invalid domain data."""


class InvalidIdentifier(MaliDomainError):
    """Raised when an identifier is not a valid opaque value."""


class InvalidSkill(MaliDomainError):
    """Raised when a skill cannot be represented safely."""


class DuplicateSkill(MaliDomainError):
    """Raised when a curriculum declares a skill more than once."""


class UnknownSkill(MaliDomainError):
    """Raised when a requirement names a skill outside its curriculum."""


class DuplicateRequirement(MaliDomainError):
    """Raised when a curriculum repeats a declared requirement."""


class SelfRequirement(MaliDomainError):
    """Raised when a skill is declared as its own requirement."""


class PrerequisiteCycle(MaliDomainError):
    """Raised when declared skill requirements contain a cycle."""


class CurriculumTooLarge(MaliDomainError):
    """Raised when a curriculum exceeds Mali's safe progress-set limit."""


class InvalidProgress(MaliDomainError):
    """Raised when a progress mask is not valid for its curriculum."""
