"""Errors and business exception markers for asynchronous tasks."""

from __future__ import annotations


class TaskError(RuntimeError):
    """Base class for task submission and execution errors."""


class TaskBackendNotStarted(TaskError):
    """Raised when a task is submitted before its runtime backend starts."""


class TaskBackendClosed(TaskError):
    """Raised when a task is submitted after the backend began shutting down."""


class TaskBackendDisabled(TaskError):
    """Raised when asynchronous task execution was explicitly disabled."""


class TaskQueueFull(TaskError):
    """Raised when a bounded in-memory queue cannot accept a task in time."""


class UnknownTaskError(TaskError):
    """Raised when no handler is registered for a task name."""


class UnsupportedTaskPayloadVersion(TaskError):
    """Raised when a task uses a payload protocol version without a handler."""


class TaskConfigContextError(TaskError):
    """Raised when request-scoped configuration cannot be safely captured."""


class RetryableTaskError(TaskError):
    """Optional marker for failures that should be retried."""


class NonRetryableTaskError(TaskError):
    """Optional marker for failures that should skip retries."""
