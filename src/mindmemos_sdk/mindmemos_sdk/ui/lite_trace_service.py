"""Read MindMemOS Lite SQLite traces for the local SDK console."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
_REQUIRED_TABLES = frozenset({"traces", "spans", "span_events", "llm_calls"})
_SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_MAX_DATABASES = 200
_MAX_SCAN_DEPTH = 6


class LiteTraceService:
    """Discover and query Lite trace databases without mutating them."""

    def list_spans(
        self,
        directory: str | Path,
        *,
        span_name: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        root = _resolve_input_path(directory)
        databases = _discover_databases(root)
        normalized_name = span_name.strip()
        if len(normalized_name) > 200:
            raise ValueError("span_name must not exceed 200 characters.")
        rows: list[dict[str, object]] = []
        total = 0
        local_limit = limit + offset
        where_sql = "WHERE instr(lower(span.name), lower(?)) > 0" if normalized_name else ""
        filter_params: tuple[object, ...] = (normalized_name,) if normalized_name else ()

        for database in databases:
            source = _source_name(root, database)
            with closing(_connect_read_only(database)) as connection:
                total += int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM spans span {where_sql}",  # noqa: S608 - fixed SQL fragment.
                        filter_params,
                    ).fetchone()[0]
                )
                database_rows = connection.execute(
                    f"""
                    SELECT
                        span.trace_id,
                        span.span_id,
                        span.parent_span_id,
                        span.name,
                        span.kind,
                        span.start_time_ns,
                        span.end_time_ns,
                        span.duration_ns,
                        span.status_code,
                        span.status_message,
                        span.service_name,
                        trace.root_span_id AS stored_root_span_id,
                        root.span_id AS resolved_root_span_id,
                        root.name AS root_span_name
                    FROM spans span
                    JOIN traces trace ON trace.trace_id = span.trace_id
                    LEFT JOIN spans root
                      ON root.trace_id = trace.trace_id
                     AND root.span_id = trace.root_span_id
                    {where_sql}
                    ORDER BY span.start_time_ns DESC, span.span_id
                    LIMIT ?
                    """,  # noqa: S608 - only the fixed optional WHERE fragment is interpolated.
                    (*filter_params, local_limit),
                ).fetchall()
                for row in database_rows:
                    start_ns = int(row["start_time_ns"])
                    end_ns = int(row["end_time_ns"])
                    root_span_id, root_span_status = _resolve_root_span_id(
                        row["stored_root_span_id"],
                        row["resolved_root_span_id"],
                    )
                    rows.append(
                        {
                            "trace_id": row["trace_id"],
                            "span_id": row["span_id"],
                            "parent_span_id": row["parent_span_id"],
                            "name": row["name"],
                            "kind": row["kind"],
                            "start_time": _ns_to_iso(start_ns),
                            "start_time_ns": str(start_ns),
                            "end_time": _ns_to_iso(end_ns),
                            "duration_ms": _ns_to_ms(int(row["duration_ns"])),
                            "status_code": row["status_code"],
                            "status_message": row["status_message"],
                            "service_name": row["service_name"],
                            "root_span_id": root_span_id,
                            "root_span_status": root_span_status,
                            "root_span_name": _root_span_name(row["root_span_name"], root_span_status),
                            "is_root": root_span_id is not None and str(row["span_id"]) == root_span_id,
                            "source": source,
                        }
                    )

        rows.sort(key=lambda item: int(str(item["start_time_ns"])), reverse=True)
        selected = rows[offset : offset + limit]
        total_pages = max(1, (total + limit - 1) // limit)
        page = offset // limit + 1
        return {
            "directory": str(root),
            "databases": [
                {
                    "source": _source_name(root, database),
                    "size_bytes": database.stat().st_size,
                }
                for database in databases
            ],
            "database_count": len(databases),
            "spans": selected,
            "count": len(selected),
            "total": total,
            "span_name": normalized_name,
            "limit": limit,
            "offset": offset,
            "page": page,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": offset + len(selected) < total,
        }

    def list_traces(
        self,
        directory: str | Path,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, object]:
        root = _resolve_input_path(directory)
        databases = _discover_databases(root)
        rows: list[dict[str, object]] = []
        total = 0
        local_limit = limit + offset

        for database in databases:
            source = _source_name(root, database)
            with closing(_connect_read_only(database)) as connection:
                total += int(connection.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
                database_rows = connection.execute(
                    """
                    SELECT
                        trace.trace_id,
                        trace.root_span_id,
                        trace.service_name,
                        trace.start_time_ns,
                        trace.end_time_ns,
                        trace.run_id,
                        trace.request_id,
                        trace.project_id,
                        trace.api_key_uuid,
                        root.span_id AS resolved_root_span_id,
                        root.name AS root_span_name,
                        root.duration_ns AS root_duration_ns,
                        root.status_code AS root_status_code,
                        (SELECT COUNT(*) FROM spans span_count
                         WHERE span_count.trace_id = trace.trace_id) AS span_count,
                        (SELECT COUNT(*) FROM llm_calls llm_count
                         WHERE llm_count.trace_id = trace.trace_id) AS llm_call_count
                    FROM traces trace
                    LEFT JOIN spans root
                      ON root.trace_id = trace.trace_id
                     AND root.span_id = trace.root_span_id
                    ORDER BY trace.start_time_ns DESC
                    LIMIT ?
                    """,
                    (local_limit,),
                ).fetchall()
                for row in database_rows:
                    start_ns = int(row["start_time_ns"])
                    end_ns = int(row["end_time_ns"])
                    duration_ns = int(row["root_duration_ns"] or max(0, end_ns - start_ns))
                    root_span_id, root_span_status = _resolve_root_span_id(
                        row["root_span_id"],
                        row["resolved_root_span_id"],
                    )
                    rows.append(
                        {
                            "trace_id": row["trace_id"],
                            "root_span_id": root_span_id,
                            "root_span_status": root_span_status,
                            "root_span_name": _root_span_name(row["root_span_name"], root_span_status),
                            "service_name": row["service_name"],
                            "start_time": _ns_to_iso(start_ns),
                            "start_time_ns": str(start_ns),
                            "end_time": _ns_to_iso(end_ns),
                            "duration_ms": _ns_to_ms(duration_ns),
                            "status_code": row["root_status_code"] or "UNSET",
                            "span_count": int(row["span_count"]),
                            "llm_call_count": int(row["llm_call_count"]),
                            "run_id": row["run_id"],
                            "request_id": row["request_id"],
                            "project_id": row["project_id"],
                            "api_key_uuid": row["api_key_uuid"],
                            "source": source,
                        }
                    )

        rows.sort(key=lambda item: int(str(item["start_time_ns"])), reverse=True)
        selected = rows[offset : offset + limit]
        return {
            "directory": str(root),
            "databases": [
                {
                    "source": _source_name(root, database),
                    "size_bytes": database.stat().st_size,
                }
                for database in databases
            ],
            "database_count": len(databases),
            "traces": selected,
            "count": len(selected),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def trace_detail(
        self,
        directory: str | Path,
        *,
        source: str,
        trace_id: str,
        selected_span_id: str | None = None,
    ) -> dict[str, object]:
        root = _resolve_input_path(directory)
        database = _resolve_database(root, source)
        with closing(_connect_read_only(database)) as connection:
            trace = connection.execute(
                """
                SELECT trace_id, root_span_id, service_name, start_time_ns, end_time_ns,
                       run_id, request_id, project_id, api_key_uuid, attributes_json
                FROM traces
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
            if trace is None:
                raise ValueError(f"Trace {trace_id!r} was not found in {source!r}.")

            span_rows = connection.execute(
                """
                SELECT trace_id, span_id, parent_span_id, name, kind, start_time_ns,
                       end_time_ns, duration_ns, status_code, status_message,
                       service_name, instrumentation_scope, attributes_json, resource_json
                FROM spans
                WHERE trace_id = ?
                ORDER BY start_time_ns, span_id
                """,
                (trace_id,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT span_id, event_index, name, timestamp_ns, attributes_json
                FROM span_events
                WHERE trace_id = ?
                ORDER BY timestamp_ns, event_index
                """,
                (trace_id,),
            ).fetchall()
            llm_rows = connection.execute(
                """
                SELECT span_id, operation, task, model, provider, prompt_tokens,
                       completion_tokens, total_tokens, duration_ns, status_code
                FROM llm_calls
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchall()

        events_by_span: dict[str, list[dict[str, object]]] = {}
        trace_start_ns = int(trace["start_time_ns"])
        trace_end_ns = int(trace["end_time_ns"])
        for event in event_rows:
            event_ns = int(event["timestamp_ns"])
            events_by_span.setdefault(str(event["span_id"]), []).append(
                {
                    "event_index": int(event["event_index"]),
                    "name": event["name"],
                    "timestamp": _ns_to_iso(event_ns),
                    "timestamp_ns": str(event_ns),
                    "offset_ms": _ns_to_ms(max(0, event_ns - trace_start_ns)),
                    "attributes": _load_json(event["attributes_json"]),
                }
            )
        llm_by_span = {
            str(row["span_id"]): {
                "operation": row["operation"],
                "task": row["task"],
                "model": row["model"],
                "provider": row["provider"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "duration_ms": _ns_to_ms(int(row["duration_ns"])),
                "status_code": row["status_code"],
            }
            for row in llm_rows
        }

        root_span_id, root_span_status = _resolve_trace_root_span(trace["root_span_id"], span_rows)
        depths = _span_depths(root_span_id, span_rows)
        spans: list[dict[str, object]] = []
        for row in span_rows:
            span_id = str(row["span_id"])
            start_ns = int(row["start_time_ns"])
            end_ns = int(row["end_time_ns"])
            attributes = _load_json(row["attributes_json"])
            spans.append(
                {
                    "trace_id": row["trace_id"],
                    "span_id": span_id,
                    "parent_span_id": row["parent_span_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "depth": depths.get(span_id, 0),
                    "start_time": _ns_to_iso(start_ns),
                    "start_time_ns": str(start_ns),
                    "end_time": _ns_to_iso(end_ns),
                    "end_time_ns": str(end_ns),
                    "start_offset_ms": _ns_to_ms(max(0, start_ns - trace_start_ns)),
                    "end_offset_ms": _ns_to_ms(max(0, end_ns - trace_start_ns)),
                    "duration_ms": _ns_to_ms(int(row["duration_ns"])),
                    "status_code": row["status_code"],
                    "status_message": row["status_message"],
                    "service_name": row["service_name"],
                    "instrumentation_scope": row["instrumentation_scope"],
                    "attributes": attributes,
                    "resource": _load_json(row["resource_json"]),
                    "events": events_by_span.get(span_id, []),
                    "llm_call": llm_by_span.get(span_id),
                    "io": _classify_io(attributes),
                }
            )

        spans_by_id = {str(span["span_id"]): span for span in spans}
        resolved_selected_span_id = selected_span_id or root_span_id or (str(spans[0]["span_id"]) if spans else None)
        if resolved_selected_span_id is not None and resolved_selected_span_id not in spans_by_id:
            raise ValueError(f"Span {resolved_selected_span_id!r} was not found in trace {trace_id!r}.")
        ancestry = _span_ancestry(
            root_span_id,
            resolved_selected_span_id,
            spans_by_id,
        )

        return {
            "directory": str(root),
            "source": _source_name(root, database),
            "trace": {
                "trace_id": trace["trace_id"],
                "root_span_id": root_span_id,
                "root_span_status": root_span_status,
                "time_range_kind": "root" if root_span_status == "resolved" else "observed",
                "service_name": trace["service_name"],
                "start_time": _ns_to_iso(trace_start_ns),
                "start_time_ns": str(trace_start_ns),
                "end_time": _ns_to_iso(trace_end_ns),
                "end_time_ns": str(trace_end_ns),
                "duration_ms": _ns_to_ms(max(0, trace_end_ns - trace_start_ns)),
                "run_id": trace["run_id"],
                "request_id": trace["request_id"],
                "project_id": trace["project_id"],
                "api_key_uuid": trace["api_key_uuid"],
                "attributes": _load_json(trace["attributes_json"]),
                "span_count": len(spans),
                "llm_call_count": len(llm_rows),
            },
            "spans": spans,
            "selected_span_id": resolved_selected_span_id,
            "root_span": _span_summary(spans_by_id.get(root_span_id)),
            "selected_span": _span_summary(spans_by_id.get(resolved_selected_span_id)),
            "ancestry": [_span_summary(span) for span in ancestry],
            "selected_connected_to_root": bool(
                ancestry and root_span_id is not None and str(ancestry[0]["span_id"]) == root_span_id
            ),
        }


def _resolve_input_path(value: str | Path) -> Path:
    raw = str(value).strip()
    if not raw:
        raise ValueError("A Lite trace directory is required.")
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"Lite trace path does not exist: {path}")
    if not path.is_dir() and not path.is_file():
        raise ValueError(f"Lite trace path is not a directory or file: {path}")
    return path


def _discover_databases(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() not in _DATABASE_SUFFIXES or not _is_trace_database(root):
            raise ValueError(f"Not a MindMemOS Lite trace database: {root}")
        return [root]

    databases: list[Path] = []
    for current_dir, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_dir)
        depth = len(current.relative_to(root).parts)
        dir_names[:] = [name for name in dir_names if name not in _SKIPPED_DIRECTORIES and depth < _MAX_SCAN_DEPTH]
        for file_name in file_names:
            candidate = current / file_name
            if candidate.suffix.lower() not in _DATABASE_SUFFIXES:
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root) or not _is_trace_database(resolved):
                continue
            databases.append(resolved)
            if len(databases) >= _MAX_DATABASES:
                break
        if len(databases) >= _MAX_DATABASES:
            break
    databases.sort(key=lambda path: str(path.relative_to(root)))
    if not databases:
        raise ValueError(f"No MindMemOS Lite trace databases were found under: {root}")
    return databases


def _resolve_database(root: Path, source: str) -> Path:
    normalized_source = source.strip()
    if not normalized_source:
        raise ValueError("A trace database source is required.")
    if root.is_file():
        database = root
        if normalized_source not in {root.name, str(root)}:
            raise ValueError("The requested trace database is outside the selected path.")
    else:
        database = (root / normalized_source).resolve()
        if not database.is_relative_to(root):
            raise ValueError("The requested trace database is outside the selected directory.")
    if not database.is_file() or not _is_trace_database(database):
        raise ValueError(f"Not a MindMemOS Lite trace database: {source}")
    return database


def _source_name(root: Path, database: Path) -> str:
    return database.name if root.is_file() else database.relative_to(root).as_posix()


def _connect_read_only(database: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise ValueError(f"Unable to read Lite trace database {database}: {exc}") from exc


def _is_trace_database(database: Path) -> bool:
    try:
        with closing(_connect_read_only(database)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
    except (OSError, ValueError, sqlite3.Error):
        return False
    return _REQUIRED_TABLES.issubset(tables)


def _resolve_trace_root_span(
    stored_root_span_id: object,
    spans: list[sqlite3.Row],
) -> tuple[str | None, str]:
    span_ids = {str(row["span_id"]) for row in spans}
    resolved_root_span_id = (
        str(stored_root_span_id)
        if stored_root_span_id is not None and str(stored_root_span_id) in span_ids
        else None
    )
    return _resolve_root_span_id(stored_root_span_id, resolved_root_span_id)


def _resolve_root_span_id(
    stored_root_span_id: object,
    resolved_root_span_id: object,
) -> tuple[str | None, str]:
    if stored_root_span_id is None:
        return None, "pending"
    if resolved_root_span_id is not None:
        return str(resolved_root_span_id), "resolved"
    return None, "missing"


def _root_span_name(value: object, status: str) -> str:
    if status == "pending":
        return "(root span pending)"
    if status == "missing":
        return "(root span unavailable)"
    return str(value)


def _span_depths(
    root_span_id: str | None,
    spans: list[sqlite3.Row],
) -> dict[str, int]:
    parents = {
        str(row["span_id"]): (str(row["parent_span_id"]) if row["parent_span_id"] is not None else None)
        for row in spans
    }
    memo: dict[str, int] = {}

    def depth(span_id: str, visiting: set[str]) -> int:
        if span_id in memo:
            return memo[span_id]
        if span_id == root_span_id:
            memo[span_id] = 0
            return 0
        parent_id = parents.get(span_id)
        if parent_id is None or parent_id not in parents or span_id in visiting:
            memo[span_id] = 1 if root_span_id is not None else 0
            return memo[span_id]
        memo[span_id] = depth(parent_id, visiting | {span_id}) + 1
        return memo[span_id]

    for current_span_id in parents:
        depth(current_span_id, set())
    return memo


def _span_ancestry(
    root_span_id: str | None,
    selected_span_id: str | None,
    spans_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if selected_span_id is None:
        return []
    path: list[dict[str, object]] = []
    visited: set[str] = set()
    current_id: str | None = selected_span_id
    while current_id is not None and current_id in spans_by_id and current_id not in visited:
        visited.add(current_id)
        span = spans_by_id[current_id]
        path.append(span)
        if current_id == root_span_id:
            break
        parent_span_id = span.get("parent_span_id")
        current_id = str(parent_span_id) if parent_span_id is not None else None
    path.reverse()
    return path


def _span_summary(span: dict[str, object] | None) -> dict[str, object] | None:
    if span is None:
        return None
    return {
        "span_id": span["span_id"],
        "parent_span_id": span["parent_span_id"],
        "name": span["name"],
        "duration_ms": span["duration_ms"],
        "status_code": span["status_code"],
        "depth": span["depth"],
    }


def _load_json(value: object) -> object:
    if not isinstance(value, str):
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}


def _classify_io(attributes: object) -> dict[str, dict[str, object]]:
    if not isinstance(attributes, dict):
        return {"input": {}, "output": {}}
    inputs: dict[str, object] = {}
    outputs: dict[str, object] = {}
    input_leaves = {"content", "input", "kwargs", "messages", "prompt", "query", "request", "text"}
    output_leaves = {"completion", "output", "response", "result"}
    for raw_key, value in attributes.items():
        key = str(raw_key)
        lowered = key.lower()
        leaf = lowered.rsplit(".", 1)[-1]
        if leaf in {"redacted", "chars"}:
            parent_leaf = lowered.rsplit(".", 2)[-2] if "." in lowered else ""
            leaf = parent_leaf
        if leaf in output_leaves:
            outputs[key] = value
        elif leaf in input_leaves or lowered.startswith(("arg.", "args.", "kwargs.")):
            inputs[key] = value
    return {"input": inputs, "output": outputs}


def _ns_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat().replace("+00:00", "Z")


def _ns_to_ms(value: int) -> float:
    return round(value / 1_000_000, 3)
