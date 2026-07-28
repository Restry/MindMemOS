"""Backend-neutral asynchronous Memory execution contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from ..core import MemoryRequest

T = TypeVar("T")


class AsyncMemoryBackend(ABC):
    """Execute SDK Memory requests without owning connection lifecycle."""

    @abstractmethod
    async def execute(self, request: MemoryRequest[T]) -> T:
        """Execute one request and return its SDK response model."""


__all__ = ["AsyncMemoryBackend"]
