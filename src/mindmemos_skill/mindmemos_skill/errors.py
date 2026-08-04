"""Exception hierarchy for the ``mindmemos_skill`` package.

All package-owned exceptions derive from :class:`MindMemOSSkillError`, so a
caller can handle Skill-runtime failures without catching unrelated provider or
Python exceptions.
"""

from __future__ import annotations


class MindMemOSSkillError(Exception):
    """Base class for all errors raised by ``mindmemos_skill`` itself."""


# A spelling-friendly alias for callers who use the Python package name rather
# than the product name.  The canonical public name follows the other
# MindMemOS packages (for example, ``MindMemOSSDKError``).
MindMemosSkillError = MindMemOSSkillError


class ConfigError(MindMemOSSkillError):
    """Base class for local Skill configuration errors."""


class ConfigNotInitializedError(ConfigError):
    """Raised when configuration is accessed before it is initialized."""

    def __init__(self) -> None:
        super().__init__("Config has not been initialized. Call init_config() first.")


class MissingConfigValueError(ConfigError):
    """Raised when a required configuration value is missing."""

    def __init__(self, field: str, reason: str = "") -> None:
        message = f"Missing required config field: '{field}'"
        if reason:
            message += f" ({reason})"
        super().__init__(message)
        self.field = field
        self.reason = reason


class InvalidConfigError(ConfigError):
    """Raised when a configuration value violates a package contract."""

    def __init__(self, field: str, support: str | None = None) -> None:
        message = f"Invalid config field: '{field}'"
        if support:
            message += f" only support ({support})"
        super().__init__(message)
        self.field = field
        self.support = support


class LLMError(MindMemOSSkillError):
    """Base class for local LLM client failures."""


class ModelEndpointNotConfiguredError(LLMError, RuntimeError):
    """Raised when a chat or embedding request has no model endpoint."""

    def __init__(self, model_type: str) -> None:
        self.model_type = model_type
        super().__init__(f"No {model_type} model endpoint configured")


class EmbeddingDimensionError(LLMError):
    """Raised when an embedding vector does not match the configured dimension."""

    def __init__(self, *, expected: int, actual: int, model: str, task: str) -> None:
        self.expected = expected
        self.actual = actual
        self.model = model
        self.task = task
        message = (
            f"Embedding dimension mismatch (task={task}, model={model}): "
            f"expected {expected} (= database.qdrant.vector_size), got {actual}. "
            "This usually means the `dimensions` request param was silently dropped by the "
            "provider or litellm (drop_params=True), or the embedding model was switched to one "
            "with a different native dimension. The Qdrant collection dimension is immutable after "
            "creation; restore the previous model, set endpoints[].dimensions to match vector_size, "
            "or drop and recreate the collection."
        )
        super().__init__(message)


class SkillError(MindMemOSSkillError):
    """Base class for Skill algorithm and runtime errors."""


class SkillConfigurationError(SkillError, ValueError):
    """Raised when a Skill runtime is constructed with invalid components."""


class SkillCapabilityUnavailableError(SkillError, RuntimeError):
    """Raised when the configured runtime does not provide an operation."""


class SkillServiceClosedError(SkillError, RuntimeError):
    """Raised when an operation is attempted after the service is closed."""


__all__ = [
    "MindMemOSSkillError",
    "MindMemosSkillError",
    "ConfigError",
    "ConfigNotInitializedError",
    "MissingConfigValueError",
    "InvalidConfigError",
    "LLMError",
    "ModelEndpointNotConfiguredError",
    "EmbeddingDimensionError",
    "SkillError",
    "SkillConfigurationError",
    "SkillCapabilityUnavailableError",
    "SkillServiceClosedError",
]
