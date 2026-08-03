"""Shared HTTP connection."""

from __future__ import annotations

from ..config import HttpConnectionConfig
from ..transport import AsyncHttpTransport
from .base import AsyncConnection


class HttpConnection(AsyncConnection):
    """Own one async HTTP transport shared by resource backends."""

    def __init__(
        self,
        config: HttpConnectionConfig,
        *,
        transport: AsyncHttpTransport | None = None,
        owns_transport: bool | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._owns_transport = transport is None if owns_transport is None else owns_transport
        self._closed = False

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory", "skills"})

    @property
    def transport(self) -> AsyncHttpTransport:
        """Return the transport borrowed by resource backends."""

        if self._transport is None:
            raise RuntimeError("HTTP connection is not open")
        return self._transport

    async def open(self) -> None:
        """HTTP clients initialize lazily and need no explicit startup."""

        if self._closed:
            raise RuntimeError("HTTP connection is closed")
        if self._transport is None:
            self._transport = AsyncHttpTransport(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout_seconds=self._config.timeout_seconds,
                max_retries=self._config.max_retries,
            )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport and self._transport is not None:
            await self._transport.aclose()


__all__ = ["HttpConnection"]
