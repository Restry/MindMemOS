"""HTTP implementation of the asynchronous Memory backend."""

from __future__ import annotations

from typing import TypeVar

from ...connections import HttpConnection
from ..core import MemoryRequest
from .base import AsyncMemoryBackend

T = TypeVar("T")


class HttpMemoryBackend(AsyncMemoryBackend):
    """Execute Memory requests over a borrowed HTTP connection."""

    def __init__(self, connection: HttpConnection) -> None:
        self._connection = connection

    async def execute(self, request: MemoryRequest[T]) -> T:
        envelope = await self._connection.transport.post_envelope(request.path, json=request.body)
        return request.parse(envelope)


__all__ = ["HttpMemoryBackend"]
