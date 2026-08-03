"""Tracing contract for the transport-neutral Lite memory service."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from mindmemos_lite.infra.observability import SQLiteSpanExporter
from mindmemos_lite.service.base import BaseMemoryService
from mindmemos_lite.service.memory import VanillaMemoryService
from mindmemos_lite.service.schema import (
    AddMemoryRequest,
    DeleteMemoryRequest,
    DreamingMemoryRequest,
    FeedbackMemoryRequest,
    GetMemoryRequest,
    RequestContext,
    SearchMemoryRequest,
    TextMessage,
    UpdateMemoryRequest,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

_SERVICE_METHODS = (
    BaseMemoryService.add,
    BaseMemoryService.search,
    VanillaMemoryService.get,
    VanillaMemoryService.update,
    VanillaMemoryService.delete,
    VanillaMemoryService.feedback,
    VanillaMemoryService.dream,
)


def _bind_service_tracers(monkeypatch, provider: TracerProvider) -> None:
    """Point the decorators' proxy tracers at a test-local provider."""

    for method in _SERVICE_METHODS:
        start = next(
            cell.cell_contents
            for cell in method.__closure__ or ()
            if callable(cell.cell_contents) and getattr(cell.cell_contents, "__name__", "") == "_start"
        )
        proxy = next(
            cell.cell_contents
            for cell in start.__closure__ or ()
            if type(cell.cell_contents).__name__ == "ProxyTracer"
        )
        monkeypatch.setattr(proxy, "_real_tracer", provider.get_tracer("test.memory-service"))


class _Recorder:
    async def record_add_input(self, *_args, **_kwargs) -> None:
        return None

    async def mark_add_completed(self, *_args, **_kwargs) -> None:
        return None

    async def mark_add_failed(self, *_args, **_kwargs) -> None:
        return None

    async def record_search(self, *_args, **_kwargs) -> None:
        return None


class _AddPipeline:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    async def add_sync(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.add"):
            return SimpleNamespace(status="ok", memories=[])


class _SearchPipeline:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    async def search(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.search"):
            return SimpleNamespace(status="ok", memories=[])


class _Persistence:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    async def list_memories(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.get"):
            return [], None

    async def update_memory(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.update"):
            return SimpleNamespace(changed=True)

    async def delete_memory(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.delete"):
            return SimpleNamespace(changed=True)


class _FeedbackPipeline:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    async def feedback_sync(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.feedback"):
            return SimpleNamespace(status="ok", message=None, actions=[])


class _DreamingPipeline:
    def __init__(self, tracer) -> None:
        self._tracer = tracer

    async def dream_sync(self, *_args, **_kwargs):
        with self._tracer.start_as_current_span("probe.dream"):
            return SimpleNamespace(status="ok", message=None)


@pytest.mark.asyncio
async def test_memory_service_operations_create_root_spans_with_child_work(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _bind_service_tracers(monkeypatch, provider)
    child_tracer = provider.get_tracer("test.memory-service.children")

    service = object.__new__(VanillaMemoryService)
    service._operation_recorder = _Recorder()
    service._algorithm_add_pipeline = _AddPipeline(child_tracer)
    service._algorithm_search_pipeline = _SearchPipeline(child_tracer)
    service._direct_add_pipeline = None
    service._direct_add_pipeline_factory = None
    service._memory_task_client = None
    service._persistence = _Persistence(child_tracer)
    service._feedback_pipeline = _FeedbackPipeline(child_tracer)
    service._dreaming_pipeline = _DreamingPipeline(child_tracer)

    context = RequestContext(
        request_id="request-1",
        account_id="account-1",
        project_id="project-1",
        api_key_uuid="key-1",
        user_id="user-1",
    )
    await service.add(context, AddMemoryRequest(messages=(TextMessage(text="hello"),)))
    await service.search(context, SearchMemoryRequest(query="hello"))
    await service.get(context, GetMemoryRequest())
    await service.update(context, UpdateMemoryRequest(memory_id="memory-1", content="updated"))
    await service.delete(context, DeleteMemoryRequest(memory_id="memory-1"))
    await service.feedback(context, FeedbackMemoryRequest())
    await service.dream(context, DreamingMemoryRequest(mode="sync"))

    spans = exporter.get_finished_spans()
    service_spans = {span.name: span for span in spans if span.name.startswith("memory.service.")}
    assert set(service_spans) == {
        "memory.service.add",
        "memory.service.search",
        "memory.service.get",
        "memory.service.update",
        "memory.service.delete",
        "memory.service.feedback",
        "memory.service.dream",
    }
    assert all(span.parent is None for span in service_spans.values())
    assert all(span.status.status_code is StatusCode.OK for span in service_spans.values())
    assert all(span.attributes["request_id"] == "request-1" for span in service_spans.values())
    assert all(span.attributes["project_id"] == "project-1" for span in service_spans.values())
    assert all(span.attributes["api_key_uuid"] == "key-1" for span in service_spans.values())

    for operation in ("add", "search", "get", "update", "delete", "feedback", "dream"):
        child = next(span for span in spans if span.name == f"probe.{operation}")
        service_span = service_spans[f"memory.service.{operation}"]
        assert child.parent is not None
        assert child.parent.span_id == service_span.context.span_id

    provider.shutdown()


@pytest.mark.asyncio
async def test_memory_service_span_is_persisted_as_sqlite_trace_root(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "traces.db"
    provider = TracerProvider(resource=Resource.create({"service.name": "mindmemos-lite-test"}))
    provider.add_span_processor(SimpleSpanProcessor(SQLiteSpanExporter(database_path)))
    _bind_service_tracers(monkeypatch, provider)

    service = object.__new__(VanillaMemoryService)
    service._persistence = _Persistence(provider.get_tracer("test.memory-service.children"))
    context = RequestContext(
        request_id="request-sqlite",
        account_id="account-1",
        project_id="project-sqlite",
        api_key_uuid="key-sqlite",
    )
    await service.get(context, GetMemoryRequest())
    provider.shutdown()

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    trace = connection.execute("SELECT trace_id, root_span_id FROM traces").fetchone()
    spans = connection.execute(
        "SELECT span_id, parent_span_id, name, attributes_json FROM spans ORDER BY start_time_ns"
    ).fetchall()
    connection.close()

    assert [span["name"] for span in spans] == ["memory.service.get", "probe.get"]
    assert trace["root_span_id"] == spans[0]["span_id"]
    assert spans[0]["parent_span_id"] is None
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]
    root_attributes = json.loads(spans[0]["attributes_json"])
    assert root_attributes["request_id"] == "request-sqlite"
    assert root_attributes["project_id"] == "project-sqlite"
    assert root_attributes["api_key_uuid"] == "key-sqlite"
