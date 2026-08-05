"""OpenTelemetry exporter adapter for pluggable observability backends."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from .backend import ObservabilityBackend
from .models import CompletedSpan, SpanEventRecord

logger = logging.getLogger(__name__)

_CONTENT_KEYS = frozenset({"content", "input", "kwargs", "messages", "output", "result", "text"})
_SECRET_FRAGMENTS = ("authorization", "credential", "password", "secret")


class BackendSpanExporter(SpanExporter):
    """Adapt any :class:`ObservabilityBackend` to OpenTelemetry."""

    def __init__(self, backend: ObservabilityBackend, *, capture_content: bool = False) -> None:
        self.backend = backend
        self._capture_content = capture_content
        self._closed = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._closed:
            return SpanExportResult.FAILURE
        try:
            records = tuple(
                record
                for span in spans
                if (record := _completed_span(span, capture_content=self._capture_content)) is not None
            )
            if records:
                self.backend.write_spans(records)
        except Exception:  # noqa: BLE001 - exporters must never break application work.
            logger.exception("failed to export OpenTelemetry spans")
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        if self._closed:
            return False
        try:
            self.backend.force_flush()
        except Exception:  # noqa: BLE001 - exporters must never break application work.
            logger.exception("failed to flush observability backend")
            return False
        return True

    def shutdown(self) -> None:
        if self._closed:
            return
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001 - exporters must never break application work.
            logger.exception("failed to close observability backend")
        finally:
            self._closed = True


def _completed_span(span: ReadableSpan, *, capture_content: bool) -> CompletedSpan | None:
    span_context = span.context
    if span_context is None or span.start_time is None or span.end_time is None:
        return None
    attributes = dict(span.attributes or {})
    resource_attributes = dict(span.resource.attributes or {})
    return CompletedSpan(
        trace_id=f"{span_context.trace_id:032x}",
        span_id=f"{span_context.span_id:016x}",
        parent_span_id=f"{span.parent.span_id:016x}" if span.parent is not None else None,
        name=span.name,
        kind=span.kind.name,
        start_time_ns=span.start_time,
        end_time_ns=span.end_time,
        status_code=span.status.status_code.name,
        status_message=span.status.description,
        service_name=str(resource_attributes.get("service.name") or "unknown"),
        instrumentation_scope=_instrumentation_scope_name(span),
        attributes=_sanitize_mapping(attributes, capture_content=capture_content),
        resource=_sanitize_mapping(resource_attributes, capture_content=False),
        events=tuple(
            SpanEventRecord(
                name=event.name,
                timestamp_ns=event.timestamp,
                attributes=_sanitize_mapping(dict(event.attributes or {}), capture_content=capture_content),
            )
            for event in span.events or ()
        ),
    )


def _sanitize_mapping(values: Mapping[str, Any], *, capture_content: bool) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for raw_key, value in values.items():
        key = str(raw_key)
        lowered = key.lower()
        if _is_secret_key(lowered):
            sanitized[key] = "<redacted>"
            continue
        leaf = lowered.rsplit(".", 1)[-1]
        if not capture_content and leaf in _CONTENT_KEYS:
            sanitized[f"{key}.redacted"] = True
            sanitized[f"{key}.chars"] = len(str(value))
            continue
        sanitized[key] = _json_value(value)
    return sanitized


def _is_secret_key(lowered_key: str) -> bool:
    if lowered_key.endswith("api_key_uuid"):
        return False
    return "api_key" in lowered_key or any(fragment in lowered_key for fragment in _SECRET_FRAGMENTS)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    return str(value)


def _instrumentation_scope_name(span: ReadableSpan) -> str | None:
    scope = getattr(span, "instrumentation_scope", None)
    if scope is None:
        scope = getattr(span, "instrumentation_info", None)
    name = getattr(scope, "name", None)
    return None if name is None else str(name)
