"""Small compatibility helpers for legacy Kafka message views."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import TaskEnvelope


def task_payload(task: TaskEnvelope | Any) -> Mapping[str, Any]:
    """Return the payload from a new envelope or a legacy ``ConsumedMessage``."""

    if isinstance(task, TaskEnvelope):
        return task.payload
    if hasattr(task, "json"):
        payload = task.json()
        if not isinstance(payload, Mapping):
            raise TypeError("task payload must be a mapping")
        return payload
    if isinstance(task, Mapping):
        return task
    raise TypeError(f"unsupported task message type: {type(task).__name__}")


def task_transport_fields(task: TaskEnvelope | Any) -> dict[str, Any]:
    """Return non-sensitive transport fields for compatibility logs."""

    if isinstance(task, TaskEnvelope):
        return {"task_id": task.task_id, "task_name": task.task_name}
    return {
        "topic": getattr(task, "topic", None),
        "offset": getattr(task, "offset", None),
    }
