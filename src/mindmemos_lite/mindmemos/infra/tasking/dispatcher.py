"""Bounded keyed dispatcher used by the in-process task backend."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from ...logging import get_logger
from .errors import TaskBackendClosed, TaskQueueFull

logger = get_logger(__name__)
ProcessFn = Callable[[Any], Awaitable[None]]


class KeyedTaskDispatcher:
    """Dispatch tasks by key while bounding unfinished work.

    A per-key FIFO and a fixed number of key runners provide strict order when
    ``per_key_max_concurrency`` is one. The buffer counts both queued and
    running tasks, so producers cannot bypass the configured memory bound.
    """

    def __init__(
        self,
        *,
        name: str,
        max_concurrency: int,
        per_key_max_concurrency: int,
        max_buffered: int,
        submit_timeout: float,
        process: ProcessFn,
        shared_global_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        _validate_positive("max_concurrency", max_concurrency)
        _validate_positive("per_key_max_concurrency", per_key_max_concurrency)
        _validate_positive("max_buffered", max_buffered)
        if submit_timeout < 0:
            raise ValueError("submit_timeout must be non-negative")
        self.name = name
        self._sem = asyncio.Semaphore(max_concurrency)
        self._per_key_max_concurrency = per_key_max_concurrency
        self._max_buffered = max_buffered
        self._submit_timeout = submit_timeout
        self._process = process
        self._shared_sem = shared_global_semaphore
        self._condition = asyncio.Condition()
        self._queues: dict[str, deque[Any]] = {}
        self._workers: dict[str, set[asyncio.Task[None]]] = {}
        self._buffered = 0
        self._running = 0
        self._closed = False

    async def submit(self, key: str, item: Any) -> None:
        deadline = asyncio.get_running_loop().time() + self._submit_timeout
        async with self._condition:
            while self._buffered >= self._max_buffered and not self._closed:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TaskQueueFull(f"task queue {self.name!r} is full")
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise TaskQueueFull(f"task queue {self.name!r} is full") from exc
            if self._closed:
                raise TaskBackendClosed(f"task queue {self.name!r} is closed")

            self._buffered += 1
            queue = self._queues.setdefault(key, deque())
            queue.append(item)
            workers = self._workers.setdefault(key, set())
            while len(workers) < self._per_key_max_concurrency:
                task = asyncio.create_task(self._run_key(key), name=f"task-dispatch-{self.name}-{key}")
                workers.add(task)

    async def _run_key(self, key: str) -> None:
        current = asyncio.current_task()
        while True:
            async with self._condition:
                queue = self._queues.get(key)
                if queue is None or not queue:
                    workers = self._workers.get(key)
                    if workers is not None and current is not None:
                        workers.discard(current)
                        if not workers:
                            self._workers.pop(key, None)
                            self._queues.pop(key, None)
                    self._condition.notify_all()
                    return
                item = queue.popleft()
                self._running += 1

            try:
                async with self._sem:
                    if self._shared_sem is None:
                        await self._process(item)
                    else:
                        async with self._shared_sem:
                            await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The executor records task failures. A dispatcher must stay
                # alive for later tasks even when a callback unexpectedly leaks
                # an exception.
                logger.exception("task dispatcher callback crashed", task_queue=self.name)
            finally:
                async with self._condition:
                    self._running -= 1
                    self._buffered -= 1
                    self._condition.notify_all()

    async def drain(self) -> None:
        while True:
            async with self._condition:
                if self._buffered == 0:
                    tasks = [task for workers in self._workers.values() for task in workers]
                    if not tasks:
                        return
                else:
                    tasks = []
                if not tasks:
                    await self._condition.wait()
                    continue
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_now(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
            tasks = [task for workers in self._workers.values() for task in workers]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._condition:
            self._workers.clear()
            self._queues.clear()
            self._buffered = 0
            self._running = 0
            self._condition.notify_all()

    @property
    def queue_depth(self) -> int:
        return max(0, self._buffered - self._running)

    @property
    def buffered(self) -> int:
        return self._buffered

    @property
    def running(self) -> int:
        return self._running

    @property
    def capacity(self) -> int:
        return self._max_buffered


def _validate_positive(name: str, value: int) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer >= 1")
