"""Bounded, non-durable task execution within one Lite process."""

from __future__ import annotations

import asyncio

from .dispatcher import KeyedTaskDispatcher
from .errors import TaskBackendClosed, TaskBackendNotStarted
from .executor import execute_task
from .models import TaskBackendHealth, TaskEnvelope, TaskReceipt
from .registry import TaskHandlerRegistry


class InMemoryTaskBackend:
    """Execute registered business tasks through one bounded keyed dispatcher."""

    name = "memory"

    def __init__(
        self,
        handlers: TaskHandlerRegistry,
        *,
        max_concurrency: int = 8,
        per_key_max_concurrency: int = 1,
        max_buffered: int = 2048,
        submit_timeout: float = 1.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self._handlers = handlers
        self._max_concurrency = max_concurrency
        self._per_key_max_concurrency = per_key_max_concurrency
        self._max_buffered = max_buffered
        self._submit_timeout = submit_timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._dispatcher: KeyedTaskDispatcher | None = None
        self._state = "new"
        self._failed_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._state == "running":
            return
        if self._state != "new":
            raise RuntimeError(f"memory task backend cannot start from state {self._state!r}")
        self._loop = asyncio.get_running_loop()
        self._dispatcher = KeyedTaskDispatcher(
            name=self.name,
            max_concurrency=self._max_concurrency,
            per_key_max_concurrency=self._per_key_max_concurrency,
            max_buffered=self._max_buffered,
            submit_timeout=self._submit_timeout,
            process=self._execute,
        )
        self._state = "running"

    async def submit(self, task: TaskEnvelope) -> TaskReceipt:
        dispatcher = self._require_running()
        self._handlers.resolve(task.task_name)
        await dispatcher.submit(task.dispatch_key or task.task_id, task)
        return TaskReceipt(task_id=task.task_id, task_name=task.task_name)

    async def _execute(self, task: TaskEnvelope) -> None:
        await execute_task(
            task,
            backend_name=self.name,
            handler=self._handlers.resolve(task.task_name),
            max_retries=self._max_retries,
            retry_base_delay=self._retry_base_delay,
            on_failure=self._record_failure,
        )

    async def _record_failure(self, _task: TaskEnvelope, _exc: Exception, _attempts: int) -> None:
        self._failed_count += 1

    async def flush(self, timeout: float | None = None) -> None:
        dispatcher = self._require_running_or_closing()
        if timeout is None:
            await dispatcher.drain()
        else:
            await asyncio.wait_for(dispatcher.drain(), timeout=timeout)

    async def health(self) -> TaskBackendHealth:
        dispatcher = self._dispatcher
        return TaskBackendHealth(
            backend=self.name,
            state=self._state,
            accepting=self._state == "running",
            queue_depth=dispatcher.queue_depth if dispatcher is not None else 0,
            capacity=dispatcher.capacity if dispatcher is not None else self._max_buffered,
            in_flight=dispatcher.running if dispatcher is not None else 0,
            failed_count=self._failed_count,
        )

    async def close(self, timeout: float | None = None) -> None:
        if self._state == "closed":
            return
        if self._state == "new":
            self._state = "closed"
            return
        self._state = "closing"
        dispatcher = self._dispatcher
        try:
            if dispatcher is not None:
                if timeout is None:
                    await dispatcher.drain()
                else:
                    await asyncio.wait_for(dispatcher.drain(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            pass
        finally:
            if dispatcher is not None:
                await dispatcher.close_now()
            self._dispatcher = None
            self._state = "closed"

    def _require_running(self) -> KeyedTaskDispatcher:
        if self._state == "new":
            raise TaskBackendNotStarted("memory task backend is not started")
        if self._state != "running" or self._dispatcher is None:
            raise TaskBackendClosed(f"memory task backend is {self._state}")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("memory task backend must be used from its owning event loop")
        return self._dispatcher

    def _require_running_or_closing(self) -> KeyedTaskDispatcher:
        if self._state not in {"running", "closing"} or self._dispatcher is None:
            raise TaskBackendNotStarted("memory task backend is not running")
        return self._dispatcher


__all__ = ["InMemoryTaskBackend"]
