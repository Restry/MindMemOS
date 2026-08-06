#!/usr/bin/env python3
"""Durable completed-turn ingestion and memory provenance sidecar.

The collector acknowledges an event only after SQLite commits it. Delivery to the
MindMemOS API is at-least-once; ``event_id`` makes client and collector retries
idempotent, while MindMemOS keeps semantic extraction/deduplication authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

DEFAULT_LEDGER = os.path.expanduser(os.getenv("MM_TURN_LEDGER", "~/.hermes/mindmemos_turn_ingest.sqlite3"))
CAPTURE_MODES = {"auto_hook", "explicit_remember", "import"}
EVENT_TYPES = {"turn", "memory"}
_FORBIDDEN_CONTEXT_KEYS = {
    "thinking",
    "reasoning",
    "tool_calls",
    "tool_logs",
    "tool_results",
    "transcript",
    "messages",
}


class IngestError(ValueError):
    """Base class for rejected collector events."""


class IdempotencyConflict(IngestError):
    """An event ID was reused with a different principal or payload."""


class LedgerFull(IngestError):
    """The bounded undelivered-event backlog has reached its limit."""


@dataclass(frozen=True)
class EnqueueResult:
    event_id: str
    status: str
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "status": self.status, "duplicate": not self.created}


@dataclass(frozen=True)
class ProcessResult:
    event_id: str
    status: str
    response: dict[str, Any] | None = None
    error: str | None = None


Delivery = Callable[[str, dict[str, Any]], dict[str, Any]]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(max(0.0, float(value)) for value in values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": round(ordered[-1], 3),
    }


def _bounded_string(value: Any, field: str, *, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise IngestError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise IngestError(f"{field} is required")
    if len(normalized) > maximum:
        raise IngestError(f"{field} exceeds {maximum} characters")
    return normalized


def _timestamp_ms(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise IngestError(f"{field} must be an ISO timestamp or epoch")
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            timestamp = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError as exc:
                raise IngestError(f"{field} is not a valid timestamp") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.timestamp()
    else:
        raise IngestError(f"{field} must be an ISO timestamp or epoch")
    if timestamp <= 0:
        raise IngestError(f"{field} must be positive")
    if timestamp < 10_000_000_000:
        timestamp *= 1000
    return int(timestamp)


def _validate_safe_context(value: Any, *, maximum_bytes: int) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise IngestError("safe_context must be an object")

    def inspect(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower() in _FORBIDDEN_CONTEXT_KEYS:
                    raise IngestError(f"safe_context may not contain {key}")
                inspect(child)
        elif isinstance(node, list):
            for child in node:
                inspect(child)

    inspect(value)
    try:
        encoded = _canonical(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IngestError("safe_context must be JSON-serializable") from exc
    if len(encoded) > maximum_bytes:
        raise IngestError(f"safe_context exceeds {maximum_bytes} bytes")
    return value


def normalize_principal(principal: Any) -> dict[str, str]:
    if hasattr(principal, "as_dict"):
        raw = principal.as_dict()
    elif isinstance(principal, Mapping):
        raw = dict(principal)
    else:
        raise IngestError("trusted principal is required")

    out: dict[str, str] = {}
    for field in (
        "client_id",
        "agent_kind",
        "instance",
        "credential_id",
        "display_name",
        "scope",
        "authority",
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise IngestError(f"principal.{field} is required")
        out[field] = value.strip()
    out["app_id"] = out["client_id"]
    out["agent_id"] = f"{out['agent_kind']}:{out['instance']}"
    return out


def _response_ok(response: dict[str, Any]) -> bool:
    code = response.get("code", "ok")
    return code == 0 or str(code).lower() in {"0", "ok", "success", "queued"}


def _memory_events(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    events = data.get("memories") or []
    return [event for event in events if isinstance(event, dict)]


class TurnLedger:
    def __init__(
        self,
        path: str = DEFAULT_LEDGER,
        *,
        user_id: str | None = None,
        max_pending: int = 50_000,
        done_retention_days: int = 30,
        capture_retention_days: int = 365,
        base_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
        stale_processing_seconds: float = 300.0,
        max_message_chars: int = 120_000,
        max_context_bytes: int = 16_384,
    ) -> None:
        self.path = os.path.expanduser(path)
        self.user_id = user_id or os.getenv("MINDMEMOS_USER", "leway")
        self.max_pending = max_pending
        self.done_retention_seconds = max(done_retention_days, 1) * 86400
        self.capture_retention_seconds = max(capture_retention_days, 1) * 86400
        self.base_backoff_seconds = max(base_backoff_seconds, 0.0)
        self.max_backoff_seconds = max(max_backoff_seconds, self.base_backoff_seconds)
        self.stale_processing_seconds = max(stale_processing_seconds, 1.0)
        self.max_message_chars = max_message_chars
        self.max_context_bytes = max_context_bytes
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.path):
            descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingest_events (
                    event_id TEXT PRIMARY KEY,
                    event_hash TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN ('turn', 'memory')),
                    capture_mode TEXT NOT NULL,
                    principal_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'error')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    processing_started_at REAL,
                    first_processing_started_at REAL,
                    processing_seconds REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    done_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_ingest_due
                    ON ingest_events(status, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS memory_lineage (
                    memory_id TEXT PRIMARY KEY,
                    origin_client_id TEXT NOT NULL,
                    origin_agent_kind TEXT NOT NULL,
                    origin_instance TEXT NOT NULL,
                    origin_credential_id TEXT NOT NULL,
                    origin_display_name TEXT NOT NULL,
                    origin_authority TEXT NOT NULL,
                    origin_capture_mode TEXT NOT NULL,
                    origin_at REAL NOT NULL,
                    last_client_id TEXT NOT NULL,
                    last_agent_kind TEXT NOT NULL,
                    last_instance TEXT NOT NULL,
                    last_credential_id TEXT NOT NULL,
                    last_display_name TEXT NOT NULL,
                    last_authority TEXT NOT NULL,
                    last_capture_mode TEXT NOT NULL,
                    last_operation TEXT NOT NULL,
                    last_event_id TEXT NOT NULL,
                    last_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lineage_last_client
                    ON memory_lineage(last_client_id, last_capture_mode, last_at);

                CREATE TABLE IF NOT EXISTS memory_contributors (
                    memory_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    agent_kind TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    last_capture_mode TEXT NOT NULL,
                    capture_modes_json TEXT NOT NULL,
                    PRIMARY KEY(memory_id, client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_contributors_client
                    ON memory_contributors(client_id, last_capture_mode, last_at);

                CREATE TABLE IF NOT EXISTS memory_captures (
                    memory_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    credential_id TEXT NOT NULL,
                    capture_mode TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    PRIMARY KEY(memory_id, event_id, client_id, capture_mode, operation)
                );
                CREATE INDEX IF NOT EXISTS idx_captures_memory
                    ON memory_captures(memory_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_captures_event
                    ON memory_captures(event_id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(ingest_events)").fetchall()}
            if "first_processing_started_at" not in columns:
                connection.execute("ALTER TABLE ingest_events ADD COLUMN first_processing_started_at REAL")
            if "processing_seconds" not in columns:
                connection.execute("ALTER TABLE ingest_events ADD COLUMN processing_seconds REAL NOT NULL DEFAULT 0")
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def submit_turn(self, payload: Mapping[str, Any], principal: Any) -> EnqueueResult:
        allowed = {
            "event_id",
            "session_id",
            "turn_id",
            "user_message",
            "assistant_message",
            "started_at",
            "completed_at",
            "safe_context",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise IngestError(f"unknown turn fields: {', '.join(sorted(unknown))}")
        normalized = {
            "event_id": _bounded_string(payload.get("event_id"), "event_id", maximum=256),
            "session_id": _bounded_string(payload.get("session_id"), "session_id", maximum=512),
            "turn_id": _bounded_string(payload.get("turn_id"), "turn_id", maximum=512),
            "user_message": _bounded_string(
                payload.get("user_message"), "user_message", maximum=self.max_message_chars
            ),
            "assistant_message": _bounded_string(
                payload.get("assistant_message"), "assistant_message", maximum=self.max_message_chars
            ),
            "started_at": _timestamp_ms(payload.get("started_at"), "started_at"),
            "completed_at": _timestamp_ms(payload.get("completed_at"), "completed_at"),
            "safe_context": _validate_safe_context(payload.get("safe_context"), maximum_bytes=self.max_context_bytes),
        }
        return self._enqueue("turn", "auto_hook", normalized, principal)

    def submit_memory(
        self,
        payload: Mapping[str, Any],
        principal: Any,
        *,
        capture_mode: str = "explicit_remember",
    ) -> EnqueueResult:
        allowed = {"event_id", "session_id", "content", "timestamp", "safe_context"}
        unknown = set(payload) - allowed
        if unknown:
            raise IngestError(f"unknown memory fields: {', '.join(sorted(unknown))}")
        normalized = {
            "event_id": _bounded_string(payload.get("event_id"), "event_id", maximum=256),
            "session_id": _bounded_string(payload.get("session_id"), "session_id", maximum=512),
            "content": _bounded_string(payload.get("content"), "content", maximum=self.max_message_chars),
            "timestamp": _timestamp_ms(payload.get("timestamp"), "timestamp"),
            "safe_context": _validate_safe_context(payload.get("safe_context"), maximum_bytes=self.max_context_bytes),
        }
        return self._enqueue("memory", capture_mode, normalized, principal)

    def _enqueue(
        self,
        event_type: str,
        capture_mode: str,
        payload: dict[str, Any],
        principal: Any,
    ) -> EnqueueResult:
        if event_type not in EVENT_TYPES:
            raise IngestError(f"unsupported event_type: {event_type}")
        if capture_mode not in CAPTURE_MODES:
            raise IngestError(f"unsupported capture_mode: {capture_mode}")
        resolved_principal = normalize_principal(principal)
        event_id = payload["event_id"]
        event_hash = hashlib.sha256(
            _canonical(
                {
                    "event_type": event_type,
                    "capture_mode": capture_mode,
                    "client_id": resolved_principal["client_id"],
                    "payload": payload,
                }
            ).encode("utf-8")
        ).hexdigest()
        now = time.time()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_hash, status FROM ingest_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if existing["event_hash"] != event_hash:
                    raise IdempotencyConflict("event_id already belongs to a different payload or client")
                return EnqueueResult(event_id, str(existing["status"]), False)
            pending_count = connection.execute("SELECT COUNT(*) FROM ingest_events WHERE status != 'done'").fetchone()[
                0
            ]
            if pending_count >= self.max_pending:
                raise LedgerFull("collector backlog is full")
            connection.execute(
                """
                INSERT INTO ingest_events(
                    event_id, event_hash, event_type, capture_mode,
                    principal_json, payload_json, status, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    event_id,
                    event_hash,
                    event_type,
                    capture_mode,
                    _canonical(resolved_principal),
                    _canonical(payload),
                    now,
                    now,
                    now,
                ),
            )
        return EnqueueResult(event_id, "pending", True)

    def _claim(self, event_id: str | None = None, *, force: bool = False) -> sqlite3.Row | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ingest_events
                   SET status = 'error', next_attempt_at = ?, processing_started_at = NULL,
                       last_error = COALESCE(last_error, 'recovered stale processing event'), updated_at = ?
                 WHERE status = 'processing' AND processing_started_at < ?
                """,
                (now, now, now - self.stale_processing_seconds),
            )
            if event_id is not None:
                if force:
                    row = connection.execute(
                        """
                        SELECT * FROM ingest_events
                         WHERE event_id = ? AND status IN ('pending', 'error')
                        """,
                        (event_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM ingest_events
                         WHERE event_id = ? AND status IN ('pending', 'error')
                           AND next_attempt_at <= ?
                        """,
                        (event_id, now),
                    ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM ingest_events
                     WHERE status IN ('pending', 'error') AND next_attempt_at <= ?
                     ORDER BY created_at, event_id LIMIT 1
                    """,
                    (now,),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE ingest_events
                   SET status = 'processing', attempt_count = attempt_count + 1,
                       processing_started_at = ?,
                       first_processing_started_at = COALESCE(first_processing_started_at, ?),
                       updated_at = ?
                 WHERE event_id = ?
                """,
                (now, now, now, row["event_id"]),
            )
            return connection.execute("SELECT * FROM ingest_events WHERE event_id = ?", (row["event_id"],)).fetchone()

    def _delivery_body(self, row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
        principal = json.loads(row["principal_json"])
        payload = json.loads(row["payload_json"])
        provenance = {
            "event_id": row["event_id"],
            "client_id": principal["client_id"],
            "agent_kind": principal["agent_kind"],
            "instance": principal["instance"],
            "credential_id": principal["credential_id"],
            "display_name": principal["display_name"],
            "authority": principal["authority"],
            "capture_mode": row["capture_mode"],
        }
        metadata: dict[str, Any] = {"provenance": provenance}
        if payload.get("safe_context"):
            metadata["safe_context"] = payload["safe_context"]

        if row["event_type"] == "turn":
            messages = [
                {
                    "role": "user",
                    "content": payload["user_message"],
                    **({"timestamp": payload["started_at"]} if payload.get("started_at") else {}),
                },
                {
                    "role": "assistant",
                    "content": payload["assistant_message"],
                    **({"timestamp": payload["completed_at"]} if payload.get("completed_at") else {}),
                },
            ]
            metadata["turn_id"] = payload["turn_id"]
        else:
            messages = [
                {
                    "role": "user",
                    "content": payload["content"],
                    **({"timestamp": payload["timestamp"]} if payload.get("timestamp") else {}),
                }
            ]

        return (
            {
                "user_id": self.user_id,
                "app_id": principal["client_id"],
                "agent_id": f"{principal['agent_kind']}:{principal['instance']}",
                "session_id": payload["session_id"],
                "messages": messages,
                "mode": "sync",
                "metadata": metadata,
            },
            principal,
            payload,
        )

    def process_next(
        self,
        deliver: Delivery,
        *,
        event_id: str | None = None,
        force: bool = False,
    ) -> ProcessResult | None:
        row = self._claim(event_id, force=force)
        if row is None:
            if event_id is None:
                return None
            status = self.event_status(event_id)
            return ProcessResult(event_id, status.get("status", "missing"), response=status.get("response"))

        body, principal, payload = self._delivery_body(row)
        try:
            response = deliver("/v1/memory/add", body)
            if not isinstance(response, dict) or not _response_ok(response):
                raise RuntimeError(f"MindMemOS add failed: {str(response)[:300]}")
            occurred_at = time.time()
            completed_ms = payload.get("completed_at") or payload.get("timestamp")
            if completed_ms:
                occurred_at = completed_ms / 1000
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._record_memory_events(
                    connection,
                    _memory_events(response),
                    principal,
                    str(row["capture_mode"]),
                    str(row["event_id"]),
                    occurred_at,
                )
                now = time.time()
                processing_seconds = max(0.0, now - float(row["processing_started_at"] or now))
                connection.execute(
                    """
                    UPDATE ingest_events
                       SET status = 'done', response_json = ?, last_error = NULL,
                           processing_seconds = processing_seconds + ?,
                           processing_started_at = NULL, done_at = ?, updated_at = ?
                     WHERE event_id = ?
                    """,
                    (_canonical(response), processing_seconds, now, now, row["event_id"]),
                )
            return ProcessResult(str(row["event_id"]), "done", response=response)
        except Exception as exc:
            attempt = int(row["attempt_count"])
            delay = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** min(max(attempt - 1, 0), 16)),
            )
            now = time.time()
            processing_seconds = max(0.0, now - float(row["processing_started_at"] or now))
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE ingest_events
                       SET status = 'error', next_attempt_at = ?, last_error = ?,
                           processing_seconds = processing_seconds + ?,
                           processing_started_at = NULL, updated_at = ?
                     WHERE event_id = ?
                    """,
                    (now + delay, error, processing_seconds, now, row["event_id"]),
                )
            return ProcessResult(str(row["event_id"]), "error", error=error)

    def record_response(
        self,
        response: dict[str, Any],
        principal: Any,
        *,
        capture_mode: str,
        event_id: str,
        occurred_at: float | None = None,
    ) -> None:
        if capture_mode not in CAPTURE_MODES:
            raise IngestError(f"unsupported capture_mode: {capture_mode}")
        resolved = normalize_principal(principal)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record_memory_events(
                connection,
                _memory_events(response),
                resolved,
                capture_mode,
                event_id,
                occurred_at or time.time(),
            )

    @staticmethod
    def _copy_related_contributors(
        connection: sqlite3.Connection,
        memory_id: str,
        related_memory_ids: list[str],
    ) -> None:
        for related_id in related_memory_ids:
            if related_id == memory_id:
                continue
            rows = connection.execute("SELECT * FROM memory_contributors WHERE memory_id = ?", (related_id,)).fetchall()
            for row in rows:
                TurnLedger._upsert_contributor(
                    connection,
                    memory_id,
                    {
                        "client_id": row["client_id"],
                        "agent_kind": row["agent_kind"],
                        "instance": row["instance"],
                        "display_name": row["display_name"],
                        "authority": row["authority"],
                    },
                    str(row["last_capture_mode"]),
                    float(row["last_at"]),
                    first_at=float(row["first_at"]),
                    capture_modes=json.loads(row["capture_modes_json"] or "[]"),
                )

    @staticmethod
    def _upsert_contributor(
        connection: sqlite3.Connection,
        memory_id: str,
        principal: Mapping[str, str],
        capture_mode: str,
        occurred_at: float,
        *,
        first_at: float | None = None,
        capture_modes: list[str] | None = None,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM memory_contributors WHERE memory_id = ? AND client_id = ?",
            (memory_id, principal["client_id"]),
        ).fetchone()
        modes = set(capture_modes or [])
        modes.add(capture_mode)
        if existing is None:
            connection.execute(
                """
                INSERT INTO memory_contributors(
                    memory_id, client_id, agent_kind, instance, display_name, authority,
                    first_at, last_at, last_capture_mode, capture_modes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    principal["client_id"],
                    principal["agent_kind"],
                    principal["instance"],
                    principal["display_name"],
                    principal["authority"],
                    first_at if first_at is not None else occurred_at,
                    occurred_at,
                    capture_mode,
                    _canonical(sorted(modes)),
                ),
            )
            return

        modes.update(json.loads(existing["capture_modes_json"] or "[]"))
        if occurred_at >= float(existing["last_at"]):
            agent_kind = principal["agent_kind"]
            instance = principal["instance"]
            display_name = principal["display_name"]
            authority = principal["authority"]
            last_at = occurred_at
            last_capture_mode = capture_mode
        else:
            agent_kind = existing["agent_kind"]
            instance = existing["instance"]
            display_name = existing["display_name"]
            authority = existing["authority"]
            last_at = existing["last_at"]
            last_capture_mode = existing["last_capture_mode"]
        connection.execute(
            """
            UPDATE memory_contributors
               SET agent_kind = ?, instance = ?, display_name = ?, authority = ?,
                   first_at = ?, last_at = ?, last_capture_mode = ?, capture_modes_json = ?
             WHERE memory_id = ? AND client_id = ?
            """,
            (
                agent_kind,
                instance,
                display_name,
                authority,
                min(float(existing["first_at"]), first_at if first_at is not None else occurred_at),
                last_at,
                last_capture_mode,
                _canonical(sorted(modes)),
                memory_id,
                principal["client_id"],
            ),
        )

    @staticmethod
    def _related_origin(connection: sqlite3.Connection, related_memory_ids: list[str]) -> sqlite3.Row | None:
        ids = [memory_id for memory_id in dict.fromkeys(related_memory_ids) if memory_id]
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        return connection.execute(
            f"SELECT * FROM memory_lineage WHERE memory_id IN ({placeholders}) ORDER BY origin_at LIMIT 1",
            ids,
        ).fetchone()

    @classmethod
    def _record_memory_events(
        cls,
        connection: sqlite3.Connection,
        events: list[dict[str, Any]],
        principal: Mapping[str, str],
        capture_mode: str,
        event_id: str,
        occurred_at: float,
    ) -> None:
        for event in events:
            memory_id = str(event.get("memory_id") or "").strip()
            if not memory_id:
                continue
            operation = str(event.get("operation") or "add").lower()
            related = [
                str(value) for value in (event.get("related_memory_ids") or []) if isinstance(value, str) and value
            ]
            if operation == "merge":
                cls._copy_related_contributors(connection, memory_id, related)

            lineage = connection.execute("SELECT * FROM memory_lineage WHERE memory_id = ?", (memory_id,)).fetchone()
            if lineage is None:
                inherited = cls._related_origin(connection, related) if operation == "merge" else None
                if inherited is None:
                    origin = {
                        "client_id": principal["client_id"],
                        "agent_kind": principal["agent_kind"],
                        "instance": principal["instance"],
                        "credential_id": principal["credential_id"],
                        "display_name": principal["display_name"],
                        "authority": principal["authority"],
                        "capture_mode": capture_mode,
                        "at": occurred_at,
                    }
                else:
                    origin = {
                        "client_id": inherited["origin_client_id"],
                        "agent_kind": inherited["origin_agent_kind"],
                        "instance": inherited["origin_instance"],
                        "credential_id": inherited["origin_credential_id"],
                        "display_name": inherited["origin_display_name"],
                        "authority": inherited["origin_authority"],
                        "capture_mode": inherited["origin_capture_mode"],
                        "at": inherited["origin_at"],
                    }
                connection.execute(
                    """
                    INSERT INTO memory_lineage(
                        memory_id,
                        origin_client_id, origin_agent_kind, origin_instance,
                        origin_credential_id, origin_display_name, origin_authority,
                        origin_capture_mode, origin_at,
                        last_client_id, last_agent_kind, last_instance,
                        last_credential_id, last_display_name, last_authority,
                        last_capture_mode, last_operation, last_event_id, last_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        origin["client_id"],
                        origin["agent_kind"],
                        origin["instance"],
                        origin["credential_id"],
                        origin["display_name"],
                        origin["authority"],
                        origin["capture_mode"],
                        origin["at"],
                        principal["client_id"],
                        principal["agent_kind"],
                        principal["instance"],
                        principal["credential_id"],
                        principal["display_name"],
                        principal["authority"],
                        capture_mode,
                        operation,
                        event_id,
                        occurred_at,
                        time.time(),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE memory_lineage
                       SET last_client_id = ?, last_agent_kind = ?, last_instance = ?,
                           last_credential_id = ?, last_display_name = ?, last_authority = ?,
                           last_capture_mode = ?, last_operation = ?, last_event_id = ?,
                           last_at = ?, updated_at = ?
                     WHERE memory_id = ?
                    """,
                    (
                        principal["client_id"],
                        principal["agent_kind"],
                        principal["instance"],
                        principal["credential_id"],
                        principal["display_name"],
                        principal["authority"],
                        capture_mode,
                        operation,
                        event_id,
                        occurred_at,
                        time.time(),
                        memory_id,
                    ),
                )

            cls._upsert_contributor(connection, memory_id, principal, capture_mode, occurred_at)
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_captures(
                    memory_id, event_id, client_id, credential_id,
                    capture_mode, authority, operation, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    event_id,
                    principal["client_id"],
                    principal["credential_id"],
                    capture_mode,
                    principal["authority"],
                    operation,
                    occurred_at,
                ),
            )

    def provenance_for(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [memory_id for memory_id in dict.fromkeys(memory_ids) if memory_id]
        if not ids:
            return {}
        output: dict[str, dict[str, Any]] = {}
        with self._connect() as connection:
            for offset in range(0, len(ids), 400):
                chunk = ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                lineages = connection.execute(
                    f"SELECT * FROM memory_lineage WHERE memory_id IN ({placeholders})", chunk
                ).fetchall()
                contributors = connection.execute(
                    f"""
                    SELECT * FROM memory_contributors
                     WHERE memory_id IN ({placeholders})
                     ORDER BY first_at, client_id
                    """,
                    chunk,
                ).fetchall()
                for row in lineages:
                    output[row["memory_id"]] = {
                        "origin": {
                            "client_id": row["origin_client_id"],
                            "agent_kind": row["origin_agent_kind"],
                            "instance": row["origin_instance"],
                            "credential_id": row["origin_credential_id"],
                            "display_name": row["origin_display_name"],
                            "authority": row["origin_authority"],
                            "capture_mode": row["origin_capture_mode"],
                            "at": row["origin_at"],
                        },
                        "last_source": {
                            "client_id": row["last_client_id"],
                            "agent_kind": row["last_agent_kind"],
                            "instance": row["last_instance"],
                            "credential_id": row["last_credential_id"],
                            "display_name": row["last_display_name"],
                            "authority": row["last_authority"],
                            "capture_mode": row["last_capture_mode"],
                            "operation": row["last_operation"],
                            "event_id": row["last_event_id"],
                            "at": row["last_at"],
                        },
                        "contributors": [],
                    }
                for row in contributors:
                    entry = output.get(row["memory_id"])
                    if entry is None:
                        continue
                    entry["contributors"].append(
                        {
                            "client_id": row["client_id"],
                            "agent_kind": row["agent_kind"],
                            "instance": row["instance"],
                            "display_name": row["display_name"],
                            "authority": row["authority"],
                            "first_at": row["first_at"],
                            "last_at": row["last_at"],
                            "last_capture_mode": row["last_capture_mode"],
                            "capture_modes": json.loads(row["capture_modes_json"] or "[]"),
                        }
                    )
        return output

    def event_status(self, event_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_id, status, attempt_count, next_attempt_at,
                       last_error, response_json, created_at, updated_at, done_at,
                       first_processing_started_at, processing_seconds
                  FROM ingest_events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return {"event_id": event_id, "status": "missing"}
        queue_seconds = (
            max(0.0, float(row["first_processing_started_at"]) - float(row["created_at"]))
            if row["first_processing_started_at"] is not None
            else None
        )
        total_seconds = (
            max(0.0, float(row["done_at"]) - float(row["created_at"])) if row["done_at"] is not None else None
        )
        return {
            "event_id": row["event_id"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "next_attempt_at": row["next_attempt_at"],
            "last_error": row["last_error"],
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "done_at": row["done_at"],
            "queue_seconds": queue_seconds,
            "processing_seconds": round(float(row["processing_seconds"]), 3),
            "total_seconds": total_seconds,
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            states = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM ingest_events GROUP BY status"
                ).fetchall()
            }
            lineage_count = connection.execute("SELECT COUNT(*) FROM memory_lineage").fetchone()[0]
            contributor_count = connection.execute("SELECT COUNT(*) FROM memory_contributors").fetchone()[0]
            recent = connection.execute(
                """
                SELECT created_at, first_processing_started_at, processing_seconds, done_at
                  FROM ingest_events
                 WHERE status = 'done' AND done_at >= ?
                   AND first_processing_started_at IS NOT NULL
                """,
                (time.time() - 86400,),
            ).fetchall()
        queue_seconds = [float(row["first_processing_started_at"]) - float(row["created_at"]) for row in recent]
        processing_seconds = [float(row["processing_seconds"]) for row in recent]
        total_seconds = [float(row["done_at"]) - float(row["created_at"]) for row in recent]
        return {
            "path": self.path,
            "states": {state: int(states.get(state, 0)) for state in ("pending", "processing", "done", "error")},
            "lineage_memories": int(lineage_count),
            "contributors": int(contributor_count),
            "max_pending": self.max_pending,
            "performance": {
                "window_hours": 24,
                "completed_events": len(recent),
                "queue_seconds": _latency_summary(queue_seconds),
                "processing_seconds": _latency_summary(processing_seconds),
                "total_seconds": _latency_summary(total_seconds),
            },
        }

    def retry_errors(self) -> int:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingest_events
                   SET status = 'pending', next_attempt_at = ?, updated_at = ?
                 WHERE status = 'error'
                """,
                (now, now),
            )
            return int(cursor.rowcount)

    def cleanup(self, *, now: float | None = None) -> dict[str, int]:
        current = now or time.time()
        with self._connect() as connection:
            done = connection.execute(
                "DELETE FROM ingest_events WHERE status = 'done' AND done_at < ?",
                (current - self.done_retention_seconds,),
            ).rowcount
            captures = connection.execute(
                "DELETE FROM memory_captures WHERE occurred_at < ?",
                (current - self.capture_retention_seconds,),
            ).rowcount
        return {"events": int(done), "captures": int(captures)}

    def purge(self, *, event_ids: list[str] | None = None, memory_ids: list[str] | None = None) -> None:
        """Remove explicitly named smoke-test records; never performs fuzzy deletion."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for event_id in event_ids or []:
                connection.execute("DELETE FROM memory_captures WHERE event_id = ?", (event_id,))
                connection.execute("DELETE FROM ingest_events WHERE event_id = ?", (event_id,))
            for memory_id in memory_ids or []:
                connection.execute("DELETE FROM memory_captures WHERE memory_id = ?", (memory_id,))
                connection.execute("DELETE FROM memory_contributors WHERE memory_id = ?", (memory_id,))
                connection.execute("DELETE FROM memory_lineage WHERE memory_id = ?", (memory_id,))


class IngestWorker:
    def __init__(
        self,
        ledger: TurnLedger,
        deliver: Delivery,
        *,
        poll_seconds: float = 1.0,
        cleanup_seconds: float = 3600.0,
    ) -> None:
        self.ledger = ledger
        self.deliver = deliver
        self.poll_seconds = poll_seconds
        self.cleanup_seconds = cleanup_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mindmemos-turn-ingest", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        next_cleanup = time.time() + self.cleanup_seconds
        while not self._stop.is_set():
            result = self.ledger.process_next(self.deliver)
            if result is None:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
            if time.time() >= next_cleanup:
                self.ledger.cleanup()
                next_cleanup = time.time() + self.cleanup_seconds


_DEFAULT_LEDGER: TurnLedger | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_ledger() -> TurnLedger:
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        if _DEFAULT_LEDGER is None:
            _DEFAULT_LEDGER = TurnLedger()
        return _DEFAULT_LEDGER


def _main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the MindMemOS durable turn ledger")
    parser.add_argument("command", choices=("status", "retry", "cleanup"), nargs="?", default="status")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    args = parser.parse_args()
    ledger = TurnLedger(args.ledger)
    if args.command == "retry":
        result: Any = {"retried": ledger.retry_errors(), **ledger.stats()}
    elif args.command == "cleanup":
        result = {"removed": ledger.cleanup(), **ledger.stats()}
    else:
        result = ledger.stats()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
