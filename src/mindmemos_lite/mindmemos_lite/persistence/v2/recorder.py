"""Operation-recorder table declarations for persistence v2."""

from __future__ import annotations

from ...infra.vector_store import FieldSpec, FieldType, IndexSpec, TableSpec
from .base import TableDefinition, column, required, schema_version_column

ADD_RECORD_TABLE = "add_record_v2"
SCHEMA_ADD_BUFFER_TABLE = "schema_add_buffer_v2"
SEARCH_RECORD_TABLE = "search_record_v2"


def _record_columns(*, primary_key: str) -> tuple[FieldSpec, ...]:
    return (
        schema_version_column(),
        required(primary_key, FieldType.UUID),
        column("request_id", FieldType.UUID),
        required("status", FieldType.TEXT),
        column("messages", FieldType.JSON, nullable=False, default=[]),
        column("memories", FieldType.JSON, nullable=False, default=[]),
        required("mode", FieldType.TEXT),
        column("metadata", FieldType.JSON, nullable=False, default={}),
        column("buffer_key", FieldType.TEXT),
        column("buffer_status", FieldType.TEXT),
        column("buffer_sequence", FieldType.INTEGER),
        column("buffered_at", FieldType.DATETIME),
        column("split_attempted", FieldType.BOOLEAN, nullable=False, default=False),
        column("split_attempted_at", FieldType.DATETIME),
        column("episode_id", FieldType.TEXT),
        column("episode_queued_at", FieldType.DATETIME),
        column("added_at", FieldType.DATETIME),
        column("added_timestamp_ms", FieldType.INTEGER),
        column("event_time", FieldType.DATETIME),
        column("event_timestamp_ms", FieldType.INTEGER),
        column("processing_at", FieldType.DATETIME),
        column("processed_at", FieldType.DATETIME),
        required("request_submitted_at", FieldType.DATETIME),
        column("task_completed_at", FieldType.DATETIME),
        column("error", FieldType.TEXT),
    )


def recorder_table_definitions() -> tuple[TableDefinition, ...]:
    """Return add, buffering, and search activity tables owned by the recorder port."""

    return (
        TableDefinition(
            port="recorder",
            spec=TableSpec(
                name=ADD_RECORD_TABLE,
                primary_key="add_record_id",
                fields=(
                    *_record_columns(primary_key="add_record_id"),
                    column("skill_bindings", FieldType.JSON, nullable=False, default=[]),
                    column("consolidation_status", FieldType.TEXT, nullable=False, default="pending"),
                    column("consolidated_at", FieldType.DATETIME),
                    column("consolidation_run_id", FieldType.TEXT),
                    column("task_id", FieldType.TEXT),
                    column("score", FieldType.FLOAT),
                    column("feedback_processed", FieldType.BOOLEAN, nullable=False, default=False),
                ),
                indexes=(
                    IndexSpec(name="add_record_v2_status_idx", fields=("status",)),
                    IndexSpec(
                        name="add_record_v2_buffer_idx",
                        fields=("buffer_status", "buffer_sequence"),
                    ),
                    IndexSpec(name="add_record_v2_task_idx", fields=("task_id",)),
                ),
            ),
        ),
        TableDefinition(
            port="recorder",
            spec=TableSpec(
                name=SCHEMA_ADD_BUFFER_TABLE,
                primary_key="schema_buffer_record_id",
                fields=(
                    *_record_columns(primary_key="schema_buffer_record_id"),
                    column("source_add_record_id", FieldType.UUID),
                    required("timestamp", FieldType.INTEGER),
                    column("force_generation", FieldType.BOOLEAN, nullable=False, default=False),
                    column("last_error", FieldType.TEXT),
                ),
                indexes=(
                    IndexSpec(name="schema_buffer_v2_status_idx", fields=("status",)),
                    IndexSpec(
                        name="schema_buffer_v2_buffer_idx",
                        fields=("buffer_status", "buffer_sequence"),
                    ),
                    IndexSpec(name="schema_buffer_v2_source_idx", fields=("source_add_record_id",)),
                ),
            ),
        ),
        TableDefinition(
            port="recorder",
            spec=TableSpec(
                name=SEARCH_RECORD_TABLE,
                primary_key="search_record_id",
                fields=(
                    schema_version_column(),
                    required("search_record_id", FieldType.UUID),
                    column("request_id", FieldType.UUID),
                    required("status", FieldType.TEXT),
                    required("query", FieldType.TEXT),
                    column("filters", FieldType.JSON),
                    column("top_k", FieldType.INTEGER),
                    required("search_pipeline", FieldType.TEXT),
                    column("agentic", FieldType.BOOLEAN, nullable=False, default=False),
                    required("max_rounds", FieldType.INTEGER),
                    column("rerank", FieldType.BOOLEAN, nullable=False, default=False),
                    column("score_threshold", FieldType.FLOAT),
                    column("memories", FieldType.JSON, nullable=False, default=[]),
                    required("request_submitted_at", FieldType.DATETIME),
                    column("task_completed_at", FieldType.DATETIME),
                    column("error", FieldType.TEXT),
                ),
                indexes=(
                    IndexSpec(name="search_record_v2_status_idx", fields=("status",)),
                    IndexSpec(name="search_record_v2_submitted_idx", fields=("request_submitted_at",)),
                ),
            ),
        ),
    )


__all__ = [
    "ADD_RECORD_TABLE",
    "SCHEMA_ADD_BUFFER_TABLE",
    "SEARCH_RECORD_TABLE",
    "recorder_table_definitions",
]
