"""Common task execution, retry, telemetry, and context handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from opentelemetry.trace import SpanKind, Status, StatusCode

from ...logging import extract_trace_context, get_logger, get_tracer
from ..retry import retry_delay
from .context import bind_task_context
from .errors import NonRetryableTaskError
from .models import TaskEnvelope
from .ports import TaskHandler

logger = get_logger(__name__)
tracer = get_tracer(__name__)
FailureFn = Callable[[TaskEnvelope, Exception, int], Awaitable[None]]


async def execute_task(
    task: TaskEnvelope,
    *,
    backend_name: str,
    handler: TaskHandler,
    max_retries: int,
    retry_base_delay: float,
    on_failure: FailureFn,
) -> None:
    """Execute one task and keep all attempts in one process span."""

    parent_context = extract_trace_context(dict(task.trace_context))
    with tracer.start_as_current_span(
        f"task.process {task.task_name}",
        context=parent_context,
        kind=SpanKind.CONSUMER,
    ) as span:
        span.set_attribute("messaging.system", backend_name)
        span.set_attribute("task.backend", backend_name)
        span.set_attribute("task.name", task.task_name)
        span.set_attribute("task.id", task.task_id)
        if task.dispatch_key is not None:
            span.set_attribute("task.dispatch_key", task.dispatch_key)
        attempt = 0
        try:
            with bind_task_context(task, backend_name=backend_name):
                while True:
                    try:
                        await handler(task)
                        span.set_attribute("task.retry_count", attempt)
                        span.set_attribute("task.result", "ok")
                        span.set_status(Status(StatusCode.OK))
                        return
                    except asyncio.CancelledError:
                        span.set_attribute("task.result", "cancelled")
                        raise
                    except NonRetryableTaskError:
                        raise
                    except Exception as exc:
                        if attempt >= max_retries:
                            raise
                        attempt += 1
                        logger.warning(
                            "task handler failed, retrying",
                            task_id=task.task_id,
                            task_name=task.task_name,
                            attempt=attempt,
                        )
                        span.add_event("task.retry", {"retry_count": attempt})
                        await asyncio.sleep(retry_delay(retry_base_delay, attempt))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            span.set_attribute("task.retry_count", attempt)
            span.set_attribute("task.result", "failed")
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            await on_failure(task, exc, attempt)
            logger.exception(
                "task failed",
                task_id=task.task_id,
                task_name=task.task_name,
                attempts=attempt + 1,
            )
