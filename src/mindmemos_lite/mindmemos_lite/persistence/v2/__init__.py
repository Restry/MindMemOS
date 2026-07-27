"""Typed, backend-neutral table declarations for MindMemOS Lite persistence v2."""

from .base import PersistencePortName, TableDefinition, build_registry, register_tables
from .memory import ENTITY_TABLE, MEMORY_TABLE, SOURCE_TABLE, memory_table_definitions
from .recorder import (
    ADD_RECORD_TABLE,
    SCHEMA_ADD_BUFFER_TABLE,
    SEARCH_RECORD_TABLE,
    recorder_table_definitions,
)
from .registry import build_v2_registry, table_definitions
from .skill import (
    SKILL_BLOB_TABLE,
    SKILL_TRACE_PENDING_TABLE,
    SKILL_TRACE_SUMMARY_TABLE,
    SKILL_VERSION_TABLE,
    skill_table_definitions,
)

__all__ = [
    "ADD_RECORD_TABLE",
    "ENTITY_TABLE",
    "MEMORY_TABLE",
    "SCHEMA_ADD_BUFFER_TABLE",
    "SEARCH_RECORD_TABLE",
    "SKILL_BLOB_TABLE",
    "SKILL_TRACE_PENDING_TABLE",
    "SKILL_TRACE_SUMMARY_TABLE",
    "SKILL_VERSION_TABLE",
    "SOURCE_TABLE",
    "PersistencePortName",
    "TableDefinition",
    "build_registry",
    "build_v2_registry",
    "memory_table_definitions",
    "recorder_table_definitions",
    "register_tables",
    "skill_table_definitions",
    "table_definitions",
]
