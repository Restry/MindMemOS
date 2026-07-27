"""Protocols shared by task clients and concrete backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .models import TaskBackendHealth, TaskEnvelope, TaskReceipt

TaskHandler = Callable[[TaskEnvelope], Awaitable[None]]


class TaskBackend(Protocol):
    """Lifecycle and submission contract implemented by task backends."""

    @property
    def name(self) -> str: ...

    async def start(self) -> None: ...

    async def submit(self, task: TaskEnvelope) -> TaskReceipt: ...

    async def flush(self, timeout: float | None = None) -> None: ...

    async def health(self) -> TaskBackendHealth: ...

    async def close(self, timeout: float | None = None) -> None: ...


class TaskClientLike(Protocol):
    """Small protocol useful for pipeline dependency injection and tests."""

    async def submit(
        self,
        task_name: str,
        payload: Mapping[str, Any],
        *,
        dispatch_key: str | None = None,
        payload_version: int = 1,
    ) -> TaskReceipt: ...
