"""Task config snapshots and execution-scope context management."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import structlog

from ...config import bind_config_overrides, get_config_overrides
from .errors import TaskConfigContextError
from .models import TaskConfigContext, TaskEnvelope, freeze_config_value, thaw_config_value

_PROCESS_RESOURCE_ROOTS = frozenset({"tasks", "database", "observability", "telemetry", "kafka"})


def capture_config_context(max_bytes: int = 65536) -> TaskConfigContext:
    """Capture a deep, bounded snapshot of the current request overrides."""

    overrides = get_config_overrides()
    if overrides is None or overrides.is_empty():
        return TaskConfigContext.empty()

    tenant_config = _copy_and_validate(overrides.tenant_config, "tenant_config")
    project_config = _copy_and_validate(overrides.project_config, "project_config")
    _reject_process_resource_overrides(tenant_config, "tenant_config")
    _reject_process_resource_overrides(project_config, "project_config")
    snapshot = TaskConfigContext(
        version=1,
        tenant_config=freeze_config_value(tenant_config),
        project_config=freeze_config_value(project_config),
    )
    encoded = json.dumps(
        {
            "version": snapshot.version,
            "tenant_config": thaw_config_value(snapshot.tenant_config),
            "project_config": thaw_config_value(snapshot.project_config),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise TaskConfigContextError(
            f"task config context exceeds configured limit ({len(encoded)} > {max_bytes} bytes)"
        )
    return snapshot


@contextmanager
def bind_task_context(task: TaskEnvelope, *, backend_name: str = "memory") -> Iterator[None]:
    """Bind task config and structured-log fields for exactly one execution."""

    fields = {
        "task_id": task.task_id,
        "task_name": task.task_name,
        "task_backend": backend_name,
    }
    if task.dispatch_key is not None:
        fields["dispatch_key"] = task.dispatch_key
    with structlog.contextvars.bound_contextvars(**fields):
        if task.config_context.is_empty():
            yield
        else:
            with bind_config_overrides(
                tenant_config=thaw_config_value(task.config_context.tenant_config),
                project_config=thaw_config_value(task.config_context.project_config),
            ):
                yield


def _copy_and_validate(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TaskConfigContextError(f"{field_name} must be a mapping")
    try:
        copied = copy.deepcopy(dict(value))
        json.dumps(copied, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaskConfigContextError(f"{field_name} must contain JSON-compatible values") from exc
    return copied


def _reject_process_resource_overrides(value: Mapping[str, Any] | None, field_name: str) -> None:
    if value is None:
        return
    forbidden = sorted(_PROCESS_RESOURCE_ROOTS.intersection(value))
    if forbidden:
        names = ", ".join(forbidden)
        raise TaskConfigContextError(f"{field_name} cannot override process resources: {names}")
