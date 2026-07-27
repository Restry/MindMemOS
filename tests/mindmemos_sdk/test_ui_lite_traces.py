"""Tests for the SDK console's read-only MindMemOS Lite trace explorer."""

from __future__ import annotations

import functools
import http.server
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.ui import server
from mindmemos_sdk.ui.lite_trace_service import LiteTraceService

_TRACE_ID = "0123456789abcdef0123456789abcdef"
_ROOT_SPAN_ID = "1111111111111111"
_CHILD_SPAN_ID = "2222222222222222"
_START_NS = 1_720_000_000_000_000_000


def _write_trace_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY,
            root_span_id TEXT,
            service_name TEXT NOT NULL,
            start_time_ns INTEGER NOT NULL,
            end_time_ns INTEGER NOT NULL,
            run_id TEXT,
            request_id TEXT,
            project_id TEXT,
            api_key_uuid TEXT,
            attributes_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            start_time_ns INTEGER NOT NULL,
            end_time_ns INTEGER NOT NULL,
            duration_ns INTEGER NOT NULL,
            status_code TEXT NOT NULL,
            status_message TEXT,
            service_name TEXT NOT NULL,
            instrumentation_scope TEXT,
            attributes_json TEXT NOT NULL,
            resource_json TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        );
        CREATE TABLE span_events (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            event_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            timestamp_ns INTEGER NOT NULL,
            attributes_json TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id, event_index)
        );
        CREATE TABLE llm_calls (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            task TEXT,
            model TEXT,
            provider TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            duration_ns INTEGER NOT NULL,
            status_code TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        );
        PRAGMA user_version = 1;
        """
    )
    connection.execute(
        """
        INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _TRACE_ID,
            _ROOT_SPAN_ID,
            "mindmemos-lite",
            _START_NS,
            _START_NS + 20_000_000,
            "run-1",
            "request-1",
            "project-1",
            "key-1",
            '{"request_id":"request-1"}',
        ),
    )
    connection.executemany(
        """
        INSERT INTO spans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                _TRACE_ID,
                _ROOT_SPAN_ID,
                None,
                "memory.add",
                "SERVER",
                _START_NS,
                _START_NS + 20_000_000,
                20_000_000,
                "OK",
                None,
                "mindmemos-lite",
                "mindmemos.service",
                json.dumps({"arg.messages": [{"role": "user", "content": "hello"}]}),
                '{"service.name":"mindmemos-lite"}',
            ),
            (
                _TRACE_ID,
                _CHILD_SPAN_ID,
                _ROOT_SPAN_ID,
                "llm.chat.provider",
                "CLIENT",
                _START_NS + 2_000_000,
                _START_NS + 14_000_000,
                12_000_000,
                "OK",
                None,
                "mindmemos-lite",
                "mindmemos.llm.chat",
                json.dumps({"result": {"answer": "world"}, "llm.model": "gpt-test"}),
                '{"service.name":"mindmemos-lite"}',
            ),
        ],
    )
    connection.execute(
        "INSERT INTO span_events VALUES (?, ?, ?, ?, ?, ?)",
        (
            _TRACE_ID,
            _CHILD_SPAN_ID,
            0,
            "llm.chat.completed",
            _START_NS + 13_000_000,
            '{"attempt":1}',
        ),
    )
    connection.execute(
        "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _TRACE_ID,
            _CHILD_SPAN_ID,
            "chat",
            "memory.add.extract",
            "gpt-test",
            "openai",
            10,
            4,
            14,
            12_000_000,
            "OK",
        ),
    )
    connection.commit()
    connection.close()


@contextmanager
def _running_ui(config_dir: Path) -> Iterator[httpx.Client]:
    token = "test-launch-token"
    handler = functools.partial(
        server._LocalUIHandler,
        directory=str(server._static_directory()),
        config_manager=ConfigManager(config_dir=config_dir),
        launch_token=token,
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
        headers={"X-MindMemOS-UI-Token": token},
    )
    try:
        yield client
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_lite_trace_service_lists_root_spans_and_builds_full_detail(tmp_path: Path) -> None:
    database = tmp_path / "run-1" / "traces.db"
    _write_trace_database(database)
    unrelated = tmp_path / "other.sqlite"
    sqlite3.connect(unrelated).close()

    service = LiteTraceService()
    payload = service.list_traces(tmp_path)

    assert payload["database_count"] == 1
    assert payload["total"] == 1
    assert payload["traces"] == [
        {
            "trace_id": _TRACE_ID,
            "root_span_id": _ROOT_SPAN_ID,
            "root_span_name": "memory.add",
            "service_name": "mindmemos-lite",
            "start_time": "2024-07-03T09:46:40Z",
            "start_time_ns": str(_START_NS),
            "end_time": "2024-07-03T09:46:40.020000Z",
            "duration_ms": 20.0,
            "status_code": "OK",
            "span_count": 2,
            "llm_call_count": 1,
            "run_id": "run-1",
            "request_id": "request-1",
            "project_id": "project-1",
            "api_key_uuid": "key-1",
            "source": "run-1/traces.db",
        }
    ]

    detail = service.trace_detail(
        tmp_path,
        source="run-1/traces.db",
        trace_id=_TRACE_ID,
    )

    assert detail["trace"]["root_span_id"] == _ROOT_SPAN_ID
    assert detail["trace"]["span_count"] == 2
    assert [span["depth"] for span in detail["spans"]] == [0, 1]
    assert detail["spans"][0]["io"]["input"]["arg.messages"][0]["content"] == "hello"
    assert detail["spans"][1]["io"]["output"] == {"result": {"answer": "world"}}
    assert detail["spans"][1]["events"][0]["name"] == "llm.chat.completed"
    assert detail["spans"][1]["llm_call"]["total_tokens"] == 14

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.close()


def test_lite_trace_service_rejects_database_outside_selected_directory(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    _write_trace_database(tmp_path / "outside.db")

    service = LiteTraceService()
    try:
        service.trace_detail(
            selected,
            source="../outside.db",
            trace_id=_TRACE_ID,
        )
    except ValueError as exc:
        assert "outside the selected directory" in str(exc)
    else:
        raise AssertionError("Expected traversal outside the selected directory to be rejected")


def test_lite_trace_http_routes_require_token_and_return_trace_tree(tmp_path: Path) -> None:
    database = tmp_path / "logs" / "traces.db"
    _write_trace_database(database)

    with _running_ui(tmp_path / "config") as client:
        list_response = client.get(
            "/api/v1/lite/traces",
            params={"directory": str(tmp_path / "logs")},
        )
        assert list_response.status_code == 200
        trace = list_response.json()["traces"][0]
        assert trace["root_span_name"] == "memory.add"

        detail_response = client.get(
            f"/api/v1/lite/traces/{_TRACE_ID}",
            params={
                "directory": str(tmp_path / "logs"),
                "source": trace["source"],
            },
        )
        assert detail_response.status_code == 200
        assert [span["name"] for span in detail_response.json()["spans"]] == [
            "memory.add",
            "llm.chat.provider",
        ]

        forbidden = client.get(
            "/api/v1/lite/traces",
            headers={"X-MindMemOS-UI-Token": "wrong"},
            params={"directory": str(tmp_path / "logs")},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"] == "forbidden"


def test_lite_trace_ui_exposes_navigation_flame_chart_and_span_details() -> None:
    static_dir = server._static_directory()
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")
    css = (static_dir / "app.css").read_text(encoding="utf-8")

    assert 'data-tab="lite"' in html
    assert 'data-lite-view="traces"' in html
    assert 'id="lite-flame-chart"' in html
    assert 'id="lite-span-detail"' in html
    assert "traceId" in javascript
    assert "renderLiteFlameChart" in javascript
    assert "renderLiteSpanDetail" in javascript
    assert ".lite-flame-block" in css
    assert ".lite-span-section" in css
