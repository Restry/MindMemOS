"""Process-local task handler registry."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import UnknownTaskError
from .ports import TaskHandler


class TaskHandlerRegistry:
    """Resolve stable business task names to backend-neutral handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_name: str, handler: TaskHandler) -> None:
        if not task_name.strip():
            raise ValueError("task_name must not be empty")
        existing = self._handlers.get(task_name)
        if existing is not None and existing is not handler:
            raise ValueError(f"task handler {task_name!r} is already registered")
        self._handlers[task_name] = handler

    def register_many(self, handlers: Iterable[tuple[str, TaskHandler]]) -> None:
        for task_name, handler in handlers:
            self.register(task_name, handler)

    def resolve(self, task_name: str) -> TaskHandler:
        try:
            return self._handlers[task_name]
        except KeyError as exc:
            raise UnknownTaskError(f"no handler registered for task {task_name!r}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def clear(self) -> None:
        self._handlers.clear()


_registry = TaskHandlerRegistry()


def get_handler_registry() -> TaskHandlerRegistry:
    return _registry


def register_handler(task_name: str, handler: TaskHandler) -> None:
    _registry.register(task_name, handler)


def reset_handlers() -> None:
    _registry.clear()
