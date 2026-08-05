"""Backend-neutral records emitted by the observability adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class SpanEventRecord:
    """One event attached to a completed span."""

    name: str
    timestamp_ns: int
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletedSpan:
    """Sanitized, storage-neutral representation of one completed span."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start_time_ns: int
    end_time_ns: int
    status_code: str
    status_message: str | None
    service_name: str
    instrumentation_scope: str | None
    attributes: dict[str, Any]
    resource: dict[str, Any]
    events: tuple[SpanEventRecord, ...]

    @property
    def duration_ns(self) -> int:
        return self.end_time_ns - self.start_time_ns
