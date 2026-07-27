"""Task submission facade used by services and pipelines."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from opentelemetry.trace import SpanKind, Status, StatusCode

from ...logging import get_logger, get_tracer, inject_trace_context
from .context import capture_config_context
from .errors import UnsupportedTaskPayloadVersion
from .models import TaskEnvelope, TaskReceipt
from .ports import TaskBackend
from .registry import TaskHandlerRegistry

logger = get_logger(__name__)
tracer = get_tracer(__name__)


class TaskClient:
    """Submit backend-neutral tasks without exposing queue or broker details."""

    def __init__(
        self,
        backend: TaskBackend,
        handlers: TaskHandlerRegistry,
        *,
        max_config_context_bytes: int = 65536,
    ) -> None:
        self._backend = backend
        self._handlers = handlers
        self._max_config_context_bytes = max_config_context_bytes

    @property
    def backend(self) -> TaskBackend:
        return self._backend

    @property
    def handlers(self) -> TaskHandlerRegistry:
        """Return the registry used to validate and dispatch submitted tasks."""

        return self._handlers

    async def submit(
        self,
        task_name: str,
        payload: Mapping[str, Any],
        *,
        dispatch_key: str | None = None,
        payload_version: int = 1,
    ) -> TaskReceipt:
        self._handlers.resolve(task_name)
        if payload_version != 1:
            raise UnsupportedTaskPayloadVersion(
                f"task {task_name!r} does not support payload_version={payload_version}; only version 1 is registered"
            )
        if not isinstance(payload, Mapping):
            raise TypeError("task payload must be a mapping")

        task_id = str(uuid4())
        with tracer.start_as_current_span(
            f"task.publish {task_name}",
            kind=SpanKind.PRODUCER,
        ) as span:
            span.set_attribute("messaging.system", self._backend.name)
            span.set_attribute("task.backend", self._backend.name)
            span.set_attribute("task.name", task_name)
            span.set_attribute("task.id", task_id)
            if dispatch_key is not None:
                span.set_attribute("task.dispatch_key", dispatch_key)
            try:
                envelope = TaskEnvelope(
                    task_id=task_id,
                    task_name=task_name,
                    payload_version=payload_version,
                    payload=copy.deepcopy(dict(payload)),
                    dispatch_key=dispatch_key,
                    submitted_at=datetime.now(UTC),
                    trace_context=copy.deepcopy(inject_trace_context()),
                    config_context=capture_config_context(self._max_config_context_bytes),
                )
                receipt = await self._backend.submit(envelope)
                span.set_attribute("task.result", "accepted")
                span.set_status(Status(StatusCode.OK))
                return receipt
            except Exception as exc:
                span.set_attribute("task.result", "rejected")
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                logger.warning("task submission failed", task_id=task_id, task_name=task_name, error=str(exc))
                raise

    async def send(
        self,
        task_name: str,
        value: Mapping[str, Any],
        *,
        dispatch_key: str | None = None,
        **_: Any,
    ) -> TaskReceipt:
        """Compatibility spelling for the former producer API.

        It remains on the neutral client temporarily so existing pipeline
        adapters can migrate without exposing Kafka types.
        """

        return await self.submit(task_name, value, dispatch_key=dispatch_key)
