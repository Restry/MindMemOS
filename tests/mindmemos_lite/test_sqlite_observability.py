import json
import sqlite3
from types import SimpleNamespace

import mindmemos_lite.llm.chat as chat_module
import pytest
from mindmemos_lite.infra.observability import (
    BackendSpanExporter,
    CompletedSpan,
    SQLiteObservabilityBackend,
    SQLiteSpanExporter,
)
from mindmemos_lite.llm.chat import LLMClient
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Status, StatusCode


def test_backend_exporter_does_not_require_sqlite(tmp_path) -> None:
    class RecordingBackend:
        def __init__(self) -> None:
            self.spans: list[CompletedSpan] = []
            self.flushes = 0
            self.closed = False

        def write_spans(self, spans) -> None:
            self.spans.extend(spans)

        def force_flush(self) -> None:
            self.flushes += 1

        def close(self) -> None:
            self.closed = True

    backend = RecordingBackend()
    exporter = BackendSpanExporter(backend)
    provider = TracerProvider(resource=Resource.create({"service.name": "skill-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with provider.get_tracer("skill").start_as_current_span("skill.trajectory.step") as span:
        span.set_attribute("input", "private trajectory content")
        span.set_attribute("authorization", "secret")
        span.add_event("skill.log", {"output": "private log content", "level": "info"})

    assert [span.name for span in backend.spans] == ["skill.trajectory.step"]
    record = backend.spans[0]
    assert record.attributes["input.redacted"] is True
    assert record.attributes["authorization"] == "<redacted>"
    assert record.events[0].name == "skill.log"
    assert record.events[0].attributes["output.redacted"] is True
    assert not (tmp_path / "traces.db").exists()
    assert exporter.force_flush()
    provider.shutdown()
    assert backend.flushes >= 1
    assert backend.closed is True


def test_sqlite_exporter_is_composed_from_sqlite_backend(tmp_path) -> None:
    exporter = SQLiteSpanExporter(tmp_path / "traces.db")

    assert isinstance(exporter, BackendSpanExporter)
    assert isinstance(exporter.backend, SQLiteObservabilityBackend)
    exporter.shutdown()


def test_sqlite_exporter_persists_generic_spans_events_and_llm_projection(tmp_path) -> None:
    database_path = tmp_path / "observability" / "traces.db"
    exporter = SQLiteSpanExporter(database_path, capture_content=False)
    provider = TracerProvider(resource=Resource.create({"service.name": "mindmemos-lite-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.sqlite-observability")

    with tracer.start_as_current_span("search.vanilla") as root:
        root.set_attribute("run_id", "run-1")
        root.set_attribute("request_id", "request-1")
        root.set_attribute("project_id", "project-1")
        root.set_attribute("api_key_uuid", "key-1")
        root.add_event(
            "llm.chat.input",
            {
                "messages": '[{"role":"user","content":"private"}]',
                "message_count": 1,
            },
        )
        root.add_event("llm.chat.parse_error", {"error.type": "ValueError"})

        with tracer.start_as_current_span("llm.chat.provider") as provider_span:
            provider_span.set_attribute("llm.call", True)
            provider_span.set_attribute("llm.operation", "chat")
            provider_span.set_attribute("llm.task", "search.answer")
            provider_span.set_attribute("llm.model", "gpt-test")
            provider_span.set_attribute("llm.usage.prompt_tokens", 10)
            provider_span.set_attribute("llm.usage.completion_tokens", 4)
            provider_span.set_attribute("llm.usage.total_tokens", 14)
            provider_span.set_status(Status(StatusCode.OK))

        root.set_status(Status(StatusCode.OK))

    provider.shutdown()

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"traces", "spans", "span_events", "llm_calls"} <= tables

    span_columns = {row["name"] for row in connection.execute("PRAGMA table_info(spans)")}
    assert "llm_model" not in span_columns
    assert "total_tokens" not in span_columns

    trace_row = connection.execute("SELECT * FROM traces").fetchone()
    assert trace_row["root_span_id"]
    assert trace_row["service_name"] == "mindmemos-lite-test"
    assert trace_row["run_id"] == "run-1"
    assert trace_row["request_id"] == "request-1"
    assert trace_row["project_id"] == "project-1"
    assert trace_row["api_key_uuid"] == "key-1"

    spans = connection.execute("SELECT name, parent_span_id, status_code FROM spans ORDER BY start_time_ns").fetchall()
    assert [row["name"] for row in spans] == [
        "search.vanilla",
        "llm.chat.provider",
    ]
    assert spans[0]["parent_span_id"] is None
    assert spans[1]["parent_span_id"] is not None
    assert all(row["status_code"] == "OK" for row in spans)

    events = connection.execute("SELECT name, attributes_json FROM span_events ORDER BY event_index").fetchall()
    input_attributes = json.loads(events[0]["attributes_json"])
    assert input_attributes["messages.redacted"] is True
    assert input_attributes["messages.chars"] > 0
    assert input_attributes["message_count"] == 1
    assert events[1]["name"] == "llm.chat.parse_error"

    llm_call = connection.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["operation"] == "chat"
    assert llm_call["task"] == "search.answer"
    assert llm_call["model"] == "gpt-test"
    assert llm_call["prompt_tokens"] == 10
    assert llm_call["completion_tokens"] == 4
    assert llm_call["total_tokens"] == 14
    assert llm_call["status_code"] == "OK"
    connection.close()


def test_sqlite_exporter_keeps_non_llm_spans_out_of_llm_projection(tmp_path) -> None:
    database_path = tmp_path / "traces.db"
    exporter = SQLiteSpanExporter(database_path)
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.sqlite-observability")

    with tracer.start_as_current_span("persistence.memory.get") as span:
        span.set_attribute("db.operation", "get")
        span.set_status(Status(StatusCode.OK))

    provider.shutdown()

    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT count(*) FROM spans").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM llm_calls").fetchone()[0] == 0
    connection.close()


@pytest.mark.asyncio
async def test_chat_parse_retry_projects_each_real_provider_call(tmp_path, monkeypatch) -> None:
    class Router:
        def __init__(self) -> None:
            self.calls = 0

        async def acompletion(self, **_kwargs):
            content = "bad" if self.calls == 0 else "good"
            self.calls += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3,
                    completion_tokens=2,
                    total_tokens=5,
                ),
                model="chat-test",
            )

    database_path = tmp_path / "traces.db"
    exporter = SQLiteSpanExporter(database_path)
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(chat_module, "tracer", provider.get_tracer("test.llm.chat"))
    router = Router()
    client = LLMClient(router, max_attempts=2)

    def parse(content: str):
        if content == "bad":
            raise ValueError("retry")
        return {"ok": True}

    response = await client.chat(
        task="memory.add.extract",
        messages=[{"role": "user", "content": "private"}],
        format_parser=parse,
    )
    provider.shutdown()

    assert response.parsed == {"ok": True}
    assert router.calls == 2
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        """
        SELECT operation, task, model, prompt_tokens, completion_tokens, total_tokens
        FROM llm_calls
        ORDER BY rowid
        """
    ).fetchall()
    assert rows == [
        ("chat", "memory.add.extract", "chat-test", 3, 2, 5),
        ("chat", "memory.add.extract", "chat-test", 3, 2, 5),
    ]
    connection.close()
