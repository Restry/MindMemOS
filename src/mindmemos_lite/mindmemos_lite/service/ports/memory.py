"""Application-facing memory service port.

The method set is intentionally one-to-one with the public memory API:
``add``, ``get``, ``delete``, ``update``, ``search``, ``feedback``, and
``dream`` correspond to the seven ``/v1/memory/*`` operations.  The port does
not know whether the caller is HTTP, SDK, or an in-process Python client.
"""

from __future__ import annotations

from typing import Protocol, TypeAlias

from ..schema import (
    AddMemoryRequest,
    AddMemoryResult,
    DeleteMemoryRequest,
    DreamingMemoryRequest,
    FeedbackMemoryRequest,
    FeedbackMemoryResult,
    GetMemoryRequest,
    MemoryListResult,
    MemoryMutationResult,
    RequestContext,
    SearchMemoryRequest,
    UpdateMemoryRequest,
)


class MemoryService(Protocol):
    """Transport-neutral memory use-case contract.

    Authentication, request-id generation, API response envelopes, and
    conversion of public filter DSLs are adapter concerns.  Implementations
    own pipeline selection, task submission for async modes, and recording of
    operation outcomes.
    """

    async def add(self, context: RequestContext, request: AddMemoryRequest) -> AddMemoryResult:
        """Add one or more messages and return generated memory events."""

        ...

    async def get(self, context: RequestContext, request: GetMemoryRequest) -> MemoryListResult:
        """Read project-scoped memories using the public filter semantics."""

        ...

    async def delete(self, context: RequestContext, request: DeleteMemoryRequest) -> MemoryMutationResult:
        """Delete one memory by its canonical memory ID."""

        ...

    async def update(self, context: RequestContext, request: UpdateMemoryRequest) -> MemoryMutationResult:
        """Replace one memory's content while preserving service semantics."""

        ...

    async def search(self, context: RequestContext, request: SearchMemoryRequest) -> MemoryListResult:
        """Search memories using fast or agentic strategy and optional rerank."""

        ...

    async def feedback(self, context: RequestContext, request: FeedbackMemoryRequest) -> FeedbackMemoryResult:
        """Apply explicit or implicit feedback and return planned actions."""

        ...

    async def dream(self, context: RequestContext, request: DreamingMemoryRequest) -> MemoryMutationResult:
        """Run or queue project-scoped memory consolidation."""
        ...


MemoryPort: TypeAlias = MemoryService


__all__ = ["MemoryPort", "MemoryService"]
