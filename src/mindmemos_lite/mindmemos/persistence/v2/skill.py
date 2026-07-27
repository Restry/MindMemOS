"""Skill-port table declarations for persistence v2."""

from __future__ import annotations

from ...infra.vector_store import FieldType, IndexSpec, TableSpec
from .base import TableDefinition, column, required, schema_version_column

SKILL_VERSION_TABLE = "skill_version_v2"
SKILL_BLOB_TABLE = "skill_blob_v2"
SKILL_TRACE_PENDING_TABLE = "skill_trace_pending_v2"
SKILL_TRACE_SUMMARY_TABLE = "skill_trace_summary_v2"


def skill_table_definitions() -> tuple[TableDefinition, ...]:
    """Return version, blob, pending-trace, and summary tables owned by the skill port."""

    return (
        TableDefinition(
            port="skill",
            spec=TableSpec(
                name=SKILL_VERSION_TABLE,
                primary_key="version_id",
                fields=(
                    schema_version_column(),
                    required("version_id", FieldType.UUID),
                    required("cloud_skill_id", FieldType.UUID),
                    required("skill_name", FieldType.TEXT),
                    required("content_hash", FieldType.TEXT),
                    column("parent_version_id", FieldType.UUID),
                    column("version_label", FieldType.TEXT),
                    required("status", FieldType.TEXT),
                    required("origin", FieldType.TEXT),
                    required("created_at", FieldType.DATETIME),
                ),
                indexes=(
                    IndexSpec(
                        name="skill_version_v2_lineage_idx",
                        fields=("cloud_skill_id", "created_at"),
                    ),
                    IndexSpec(name="skill_version_v2_content_idx", fields=("content_hash",)),
                ),
            ),
        ),
        TableDefinition(
            port="skill",
            spec=TableSpec(
                name=SKILL_BLOB_TABLE,
                primary_key="blob_id",
                fields=(
                    schema_version_column(),
                    required("blob_id", FieldType.UUID),
                    required("content_hash", FieldType.TEXT),
                    required("content", FieldType.TEXT),
                    required("created_at", FieldType.DATETIME),
                ),
                indexes=(
                    IndexSpec(
                        name="skill_blob_v2_content_uq",
                        fields=("content_hash",),
                        unique=True,
                    ),
                ),
            ),
        ),
        TableDefinition(
            port="skill",
            spec=TableSpec(
                name=SKILL_TRACE_PENDING_TABLE,
                primary_key="trace_point_id",
                fields=(
                    schema_version_column(),
                    required("trace_point_id", FieldType.UUID),
                    required("trace_id", FieldType.UUID),
                    required("content_hash", FieldType.TEXT),
                    column("base_version_id", FieldType.UUID),
                    column("add_record_id", FieldType.UUID),
                    required("created_at", FieldType.DATETIME),
                ),
                indexes=(
                    IndexSpec(
                        name="skill_pending_v2_content_idx",
                        fields=("content_hash", "base_version_id"),
                    ),
                    IndexSpec(name="skill_pending_v2_add_record_idx", fields=("add_record_id",)),
                ),
            ),
        ),
        TableDefinition(
            port="skill",
            spec=TableSpec(
                name=SKILL_TRACE_SUMMARY_TABLE,
                primary_key="summary_id",
                fields=(
                    schema_version_column(),
                    required("summary_id", FieldType.UUID),
                    required("cloud_skill_id", FieldType.UUID),
                    required("add_record_id", FieldType.UUID),
                    required("skill_name", FieldType.TEXT),
                    required("summary", FieldType.TEXT),
                    required("created_at", FieldType.DATETIME),
                    column("consumed_version_id", FieldType.UUID),
                    column("score", FieldType.FLOAT),
                    column("task_id", FieldType.TEXT),
                ),
                indexes=(
                    IndexSpec(
                        name="skill_summary_v2_pending_idx",
                        fields=("cloud_skill_id", "consumed_version_id"),
                    ),
                    IndexSpec(name="skill_summary_v2_task_idx", fields=("task_id",)),
                ),
            ),
        ),
    )


__all__ = [
    "SKILL_BLOB_TABLE",
    "SKILL_TRACE_PENDING_TABLE",
    "SKILL_TRACE_SUMMARY_TABLE",
    "SKILL_VERSION_TABLE",
    "skill_table_definitions",
]
