"""Memory operation errors."""

from .base import MindMemOSError


class MemoryUpdateError(MindMemOSError):
    """Raised when an in-place memory update cannot be completed safely."""


class MemoryExtractionError(MindMemOSError):
    """Semantic extraction failed; raw evidence must not enter recall storage."""

    error_code = "memory_extraction_failed"

    def __init__(
        self,
        message: str,
        *,
        cause: Exception | None = None,
        chunk_index: int | None = None,
        boundary: str | None = None,
        attempts: int = 1,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.chunk_index = chunk_index
        self.boundary = boundary
        self.attempts = attempts
        self.retryable = retryable

    def details(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "error_stage": "extract",
            "chunk_index": self.chunk_index,
            "boundary": self.boundary,
            "attempts": self.attempts,
            "retryable": self.retryable,
        }
