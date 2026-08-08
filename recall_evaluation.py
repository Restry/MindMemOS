"""Read-only recall audit views plus an isolated human-review sidecar."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime
from typing import Any, Callable

CALL_VERDICTS = {"useful", "poor", "expected_empty"}
MEMORY_LABELS = {"relevant", "partial", "irrelevant", "stale", "conflict"}


def _iso(value: Any) -> str:
    return str(value or "")


def _timestamp(value: Any) -> float:
    text = _iso(value)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _latency_ms(start: Any, end: Any) -> int | None:
    left, right = _timestamp(start), _timestamp(end)
    if not left or not right or right < left:
        return None
    return round((right - left) * 1000)


def _safe_actor(payload: dict[str, Any]) -> str:
    for key in ("agent_id", "app_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:80]
    key_id = str(payload.get("api_key_uuid") or "").strip()
    return f"KEY · {key_id[:8]}" if key_id else "UNKNOWN CLIENT"


class RecallReviewStore:
    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS call_reviews (
                    search_record_id TEXT PRIMARY KEY,
                    verdict TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS memory_reviews (
                    search_record_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (search_record_id, memory_id)
                );
                """
            )
        os.chmod(self.path, 0o600)

    def save(self, payload: dict[str, Any]) -> None:
        record_id = str(payload.get("search_record_id") or "").strip()
        if not record_id or len(record_id) > 200:
            raise ValueError("search_record_id 不正确")
        verdict = str(payload.get("verdict") or "").strip()
        reason = str(payload.get("reason") or "").strip()[:120]
        note = str(payload.get("note") or "").strip()[:1000]
        memory_reviews = payload.get("memory_reviews") or []
        if verdict and verdict not in CALL_VERDICTS:
            raise ValueError("不支持的召回评价")
        if not isinstance(memory_reviews, list) or len(memory_reviews) > 100:
            raise ValueError("记忆评价格式不正确")
        with self._connect() as connection:
            if verdict:
                connection.execute(
                    """INSERT INTO call_reviews(search_record_id, verdict, reason, note, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(search_record_id) DO UPDATE SET
                    verdict=excluded.verdict, reason=excluded.reason,
                    note=excluded.note, updated_at=CURRENT_TIMESTAMP""",
                    (record_id, verdict, reason, note),
                )
            for item in memory_reviews:
                if not isinstance(item, dict):
                    raise ValueError("记忆评价格式不正确")
                memory_id = str(item.get("memory_id") or "").strip()
                label = str(item.get("label") or "").strip()
                memory_note = str(item.get("note") or "").strip()[:500]
                if not memory_id or len(memory_id) > 200 or label not in MEMORY_LABELS:
                    raise ValueError("记忆评价内容不正确")
                connection.execute(
                    """INSERT INTO memory_reviews(search_record_id, memory_id, label, note, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(search_record_id, memory_id) DO UPDATE SET
                    label=excluded.label, note=excluded.note, updated_at=CURRENT_TIMESTAMP""",
                    (record_id, memory_id, label, memory_note),
                )

    def load(self, record_ids: list[str]) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
        if not record_ids:
            return {}, {}
        marks = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            calls = {
                row["search_record_id"]: dict(row)
                for row in connection.execute(
                    f"SELECT * FROM call_reviews WHERE search_record_id IN ({marks})", record_ids
                )
            }
            memories: dict[str, dict[str, dict]] = {}
            for row in connection.execute(
                f"SELECT * FROM memory_reviews WHERE search_record_id IN ({marks})", record_ids
            ):
                memories.setdefault(row["search_record_id"], {})[row["memory_id"]] = dict(row)
        return calls, memories


def build_recall_snapshot(
    points: list[dict[str, Any]], store: RecallReviewStore, *, limit: int = 100
) -> dict[str, Any]:
    normalized = []
    for point in points:
        payload = point.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get("query"):
            continue
        record_id = str(payload.get("search_record_id") or point.get("id") or "")
        raw_memories = payload.get("memories")
        memories = raw_memories if isinstance(raw_memories, list) else []
        items = []
        for rank, memory in enumerate(memories, 1):
            if not isinstance(memory, dict):
                continue
            text = str(memory.get("memory") or "")
            items.append(
                {
                    "memory_id": str(memory.get("id") or f"rank-{rank}"),
                    "rank": rank,
                    "memory_type": str(memory.get("memory_type") or "fact"),
                    "content": text,
                    "chars": len(text),
                    "score": memory.get("score"),
                }
            )
        normalized.append(
            {
                "search_record_id": record_id,
                "query": str(payload.get("query") or ""),
                "occurred_at": _iso(payload.get("request_submitted_at")),
                "completed_at": _iso(payload.get("task_completed_at")),
                "latency_ms": _latency_ms(
                    payload.get("request_submitted_at"), payload.get("task_completed_at")
                ),
                "status": str(payload.get("status") or "unknown"),
                "actor": _safe_actor(payload),
                "algorithm": str(payload.get("memory_algorithm") or ""),
                "pipeline": str(payload.get("search_pipeline") or ""),
                "rerank": bool(payload.get("rerank")),
                "top_k": payload.get("top_k"),
                "memories": items,
                "memory_count": len(items),
                "context_chars": sum(item["chars"] for item in items),
            }
        )
    normalized.sort(key=lambda item: _timestamp(item["occurred_at"]), reverse=True)
    records = normalized[: max(1, min(int(limit), 500))]
    call_reviews, memory_reviews = store.load([item["search_record_id"] for item in records])
    for record in records:
        rid = record["search_record_id"]
        record["review"] = call_reviews.get(rid)
        reviewed = memory_reviews.get(rid, {})
        for memory in record["memories"]:
            memory["review"] = reviewed.get(memory["memory_id"])

    verdicts = Counter(
        record["review"]["verdict"] for record in records if record.get("review")
    )
    labeled = [
        memory
        for record in records
        for memory in record["memories"]
        if memory.get("review")
    ]
    relevant_chars = sum(
        memory["chars"]
        for memory in labeled
        if memory["review"]["label"] in {"relevant", "partial"}
    )
    irrelevant_chars = sum(
        memory["chars"]
        for memory in labeled
        if memory["review"]["label"] in {"irrelevant", "stale", "conflict"}
    )
    judged_chars = relevant_chars + irrelevant_chars
    technical_success = sum(record["status"] in {"ok", "completed"} for record in records)
    return {
        "ok": True,
        "records": records,
        "summary": {
            "calls": len(records),
            "technical_success": technical_success,
            "technical_success_rate": round(technical_success / len(records) * 100, 1)
            if records
            else None,
            "reviewed_calls": sum(verdicts.values()),
            "useful_calls": verdicts["useful"],
            "poor_calls": verdicts["poor"],
            "expected_empty_calls": verdicts["expected_empty"],
            "labeled_memories": len(labeled),
            "context_waste_rate": round(irrelevant_chars / judged_chars * 100, 1)
            if judged_chars
            else None,
        },
        "read_only_observation": True,
    }
