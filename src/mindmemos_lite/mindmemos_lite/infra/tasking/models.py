"""Immutable task envelope and runtime status models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskConfigContext:
    """Versioned user/project override snapshot carried by one task."""

    version: int = 1
    tenant_config: Mapping[str, Any] | None = None
    project_config: Mapping[str, Any] | None = None

    @classmethod
    def empty(cls) -> "TaskConfigContext":
        return cls()

    def is_empty(self) -> bool:
        return not self.tenant_config and not self.project_config


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    """Backend-neutral task message.

    The payload is copied by :class:`~mindmemos_lite.infra.tasking.TaskClient` before the
    envelope is built. ``trace_context`` and ``config_context`` are carriers or
    snapshots, never live OpenTelemetry or request objects.
    """

    task_id: str
    task_name: str
    payload_version: int
    payload: Mapping[str, Any]
    dispatch_key: str | None
    submitted_at: datetime
    trace_context: Mapping[str, str]
    config_context: TaskConfigContext


@dataclass(frozen=True, slots=True)
class TaskReceipt:
    """Acknowledgement that a backend accepted a task into its queue."""

    task_id: str
    task_name: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class TaskBackendHealth:
    """Point-in-time status of a task backend."""

    backend: str
    state: str
    accepting: bool
    queue_depth: int
    capacity: int
    in_flight: int
    failed_count: int


def freeze_config_value(value: Any) -> Any:
    """Recursively freeze JSON-like config data for a task-owned snapshot."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_config_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_config_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_config_value(item) for item in value)
    return value


def thaw_config_value(value: Any) -> Any:
    """Convert a frozen config snapshot back to ordinary JSON-like values."""

    if isinstance(value, Mapping):
        return {str(key): thaw_config_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_config_value(item) for item in value]
    if isinstance(value, frozenset):
        return [thaw_config_value(item) for item in value]
    return value
