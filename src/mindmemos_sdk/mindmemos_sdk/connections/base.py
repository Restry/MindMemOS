"""Connection lifecycle contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AsyncConnection(ABC):
    """Shared asynchronous connection owned by a root SDK client."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        """Return resource names supported by this connection."""

    @abstractmethod
    async def open(self) -> None:
        """Open resources once."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close owned resources once."""


__all__ = ["AsyncConnection"]
