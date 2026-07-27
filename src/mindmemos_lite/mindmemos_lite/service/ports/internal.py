"""Private read-only memory data-plane port.

This port mirrors the two internal Console/BFF operations from the original
API.  It is deliberately separate from :mod:`.memory`: internal callers need
raw project-memory payloads and cursor pagination, while the public memory
surface returns algorithm-level ``MemoryItem`` values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypeAlias

from ..schema import (
    InternalMemoryDetailRequest,
    InternalMemoryListRequest,
    InternalMemoryPage,
    RequestContext,
)


class InternalMemoryService(Protocol):
    """Private project memory inspection contract.

    Implementations must enforce that ``request.project_id`` is the same
    project authorized by ``context`` before reading or returning a record.
    ``None`` from ``get_project_memory`` maps to the original 404 response.
    """

    async def list_project_memories(
        self,
        context: RequestContext,
        request: InternalMemoryListRequest,
    ) -> InternalMemoryPage:
        """List raw memory payloads with optional text query and cursor."""

        ...

    async def get_project_memory(
        self,
        context: RequestContext,
        request: InternalMemoryDetailRequest,
    ) -> Mapping[str, Any] | None:
        """Read one raw project memory payload, or return ``None`` if absent."""

        ...


InternalMemoryPort: TypeAlias = InternalMemoryService


__all__ = ["InternalMemoryPort", "InternalMemoryService"]
