"""Backend-neutral storage port for completed observability spans."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import CompletedSpan


class ObservabilityBackend(Protocol):
    """Synchronous storage contract used by OpenTelemetry exporters.

    Backends receive completed spans and own their storage representation. This
    keeps callers independent from SQLite and allows another package to persist
    logs/trajectory data in its own store.
    """

    def write_spans(self, spans: Sequence[CompletedSpan]) -> None:
        """Persist one exporter batch atomically when the backend supports it."""
        ...

    def force_flush(self) -> None:
        """Flush buffered writes to durable storage."""
        ...

    def close(self) -> None:
        """Release backend-owned resources. Must be idempotent."""
        ...
