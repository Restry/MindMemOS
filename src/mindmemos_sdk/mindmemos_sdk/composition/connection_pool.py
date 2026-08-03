"""Lifecycle owner for named SDK connections."""

from __future__ import annotations

import asyncio

from ..connections import AsyncConnection


class ConnectionPool:
    """Open, resolve and close named connections exactly once."""

    def __init__(self, connections: dict[str, AsyncConnection]) -> None:
        self._connections = dict(connections)
        self._opened: list[AsyncConnection] = []
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def get(self, name: str, *, capability: str | None = None) -> AsyncConnection:
        try:
            connection = self._connections[name]
        except KeyError as exc:
            raise ValueError(f"unknown SDK connection: {name!r}") from exc
        if capability is not None and capability not in connection.capabilities:
            raise ValueError(f"SDK connection {name!r} does not support {capability!r}")
        return connection

    async def open(self) -> None:
        if self._closed:
            raise RuntimeError("connection pool is closed")
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None:
            if loop is not self._owner_loop:
                raise RuntimeError("connection pool cannot be used across event loops")
            return
        self._owner_loop = loop
        try:
            for connection in self._connections.values():
                await connection.open()
                self._opened.append(connection)
        except BaseException:
            await self._close_opened()
            self._owner_loop = None
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._require_owner_loop()
        self._closed = True
        await self._close_opened()

    async def _close_opened(self) -> None:
        first_error: BaseException | None = None
        for connection in reversed(self._opened):
            try:
                await connection.aclose()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._opened.clear()
        if first_error is not None:
            raise first_error

    def _require_owner_loop(self) -> None:
        if self._owner_loop is None:
            return
        if asyncio.get_running_loop() is not self._owner_loop:
            raise RuntimeError("connection pool cannot be closed from another event loop")


__all__ = ["ConnectionPool"]
