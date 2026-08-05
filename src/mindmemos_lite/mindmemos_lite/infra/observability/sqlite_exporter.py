"""Backward-compatible SQLite OpenTelemetry exporter."""

from __future__ import annotations

from pathlib import Path

from .exporter import BackendSpanExporter
from .sqlite_backend import SQLiteObservabilityBackend


class SQLiteSpanExporter(BackendSpanExporter):
    """Wire :class:`SQLiteObservabilityBackend` to OpenTelemetry."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int | None = 14,
        capture_content: bool = False,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        backend = SQLiteObservabilityBackend(
            path,
            retention_days=retention_days,
            busy_timeout_seconds=busy_timeout_seconds,
        )
        super().__init__(backend, capture_content=capture_content)

    @property
    def path(self) -> Path:
        """Expose the database path retained by the legacy exporter API."""
        backend = self.backend
        assert isinstance(backend, SQLiteObservabilityBackend)
        return backend.path
