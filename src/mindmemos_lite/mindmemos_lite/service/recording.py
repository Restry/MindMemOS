"""Best-effort operation-record helpers owned by the service layer."""

from __future__ import annotations

from typing import Any, Awaitable

from ..logging import get_logger

logger = get_logger(__name__)


async def suppress_recording_errors(awaitable: Awaitable[Any], *, operation: str) -> Any | None:
    try:
        return await awaitable
    except Exception:
        logger.warning("memory_operation_recording_failed", operation=operation, exc_info=True)
        return None


__all__ = ["suppress_recording_errors"]
