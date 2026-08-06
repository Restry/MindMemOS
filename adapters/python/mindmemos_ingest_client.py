#!/usr/bin/env python3
"""Dependency-free durable client for MindMemOS runtime adapters."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlushResult:
    event_id: str
    status: str
    error: str | None = None


def stable_event_id(prefix: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_key_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


class DurableIngestClient:
    def __init__(
        self,
        service_url: str,
        token: str,
        spool_path: str,
        *,
        timeout: float = 2.0,
        max_pending: int = 10_000,
        base_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
        done_retention_days: int = 7,
    ) -> None:
        self.service_url = service_url.rstrip("/")
        self.token = token.strip()
        self.spool_path = os.path.expanduser(spool_path)
        self.timeout = timeout
        self.max_pending = max_pending
        self.base_backoff_seconds = max(base_backoff_seconds, 0.0)
        self.max_backoff_seconds = max(max_backoff_seconds, self.base_backoff_seconds)
        self.done_retention_seconds = max(done_retention_days, 1) * 86400
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.spool_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        os.makedirs(os.path.dirname(self.spool_path) or ".", exist_ok=True)
        if not os.path.exists(self.spool_path):
            descriptor = os.open(self.spool_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.spool_path, 0o600)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_hash TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'error')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    processing_started_at REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    done_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_client_events_due
                    ON events(status, next_attempt_at, created_at);
                """
            )
        for candidate in (self.spool_path, self.spool_path + "-wal", self.spool_path + "-shm"):
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def enqueue(self, endpoint: str, payload: dict[str, Any]) -> bool:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("payload.event_id is required")
        if not endpoint.startswith("/"):
            raise ValueError("endpoint must start with /")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(f"{endpoint}\0{payload_json}".encode()).hexdigest()
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT event_hash FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                if existing["event_hash"] != event_hash:
                    raise ValueError("event_id already has a different payload")
                return False
            pending = connection.execute("SELECT COUNT(*) FROM events WHERE status != 'done'").fetchone()[0]
            if pending >= self.max_pending:
                raise RuntimeError("local MindMemOS spool is full")
            connection.execute(
                """
                INSERT INTO events(
                    event_id, event_hash, endpoint, payload_json, status,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (event_id, event_hash, endpoint, payload_json, now, now, now),
            )
        self._wake.set()
        return True

    def _claim(self) -> sqlite3.Row | None:
        if not self.token:
            return None
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE events
                   SET status = 'error', next_attempt_at = ?, processing_started_at = NULL,
                       last_error = COALESCE(last_error, 'recovered stale sender'), updated_at = ?
                 WHERE status = 'processing' AND processing_started_at < ?
                """,
                (now, now, now - 60),
            )
            row = connection.execute(
                """
                SELECT * FROM events
                 WHERE status IN ('pending', 'error') AND next_attempt_at <= ?
                 ORDER BY created_at, event_id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE events
                   SET status = 'processing', attempt_count = attempt_count + 1,
                       processing_started_at = ?, updated_at = ?
                 WHERE event_id = ?
                """,
                (now, now, row["event_id"]),
            )
            return connection.execute("SELECT * FROM events WHERE event_id = ?", (row["event_id"],)).fetchone()

    def flush_once(self) -> FlushResult | None:
        row = self._claim()
        if row is None:
            return None
        event_id = str(row["event_id"])
        try:
            request = urllib.request.Request(
                f"{self.service_url}{row['endpoint']}",
                data=str(row["payload_json"]).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read() or b"{}")
                if response.status not in (200, 201, 202) or not body.get("ok"):
                    raise RuntimeError(f"collector rejected event: HTTP {response.status}")
            now = time.time()
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE events
                       SET status = 'done', processing_started_at = NULL,
                           last_error = NULL, done_at = ?, updated_at = ?
                     WHERE event_id = ?
                    """,
                    (now, now, event_id),
                )
            return FlushResult(event_id, "done")
        except Exception as exc:
            attempt = int(row["attempt_count"])
            delay = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** min(max(attempt - 1, 0), 16)),
            )
            now = time.time()
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE events
                       SET status = 'error', next_attempt_at = ?, processing_started_at = NULL,
                           last_error = ?, updated_at = ?
                     WHERE event_id = ?
                    """,
                    (now + delay, error, now, event_id),
                )
            return FlushResult(event_id, "error", error)

    def flush(self, limit: int = 20) -> list[FlushResult]:
        results: list[FlushResult] = []
        for _ in range(max(limit, 0)):
            result = self.flush_once()
            if result is None:
                break
            results.append(result)
            if result.status == "error":
                break
        self.cleanup()
        return results

    def cleanup(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM events WHERE status = 'done' AND done_at < ?",
                (time.time() - self.done_retention_seconds,),
            )
            return int(cursor.rowcount)

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            found = {
                row["status"]: int(row["count"])
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM events GROUP BY status").fetchall()
            }
        return {state: found.get(state, 0) for state in ("pending", "processing", "done", "error")}

    def start_worker(self, poll_seconds: float = 2.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.is_set():
                if self.flush_once() is None:
                    self._wake.wait(poll_seconds)
                    self._wake.clear()

        self._thread = threading.Thread(target=run, name="mindmemos-adapter-spool", daemon=True)
        self._thread.start()

    def stop_worker(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
