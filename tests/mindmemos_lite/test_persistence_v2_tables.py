from __future__ import annotations

import pytest
from mindmemos.infra.vector_store import TableRegistry
from mindmemos.persistence.v2 import (
    ADD_RECORD_TABLE,
    ENTITY_TABLE,
    MEMORY_TABLE,
    SCHEMA_ADD_BUFFER_TABLE,
    SEARCH_RECORD_TABLE,
    SKILL_BLOB_TABLE,
    SKILL_TRACE_PENDING_TABLE,
    SKILL_TRACE_SUMMARY_TABLE,
    SKILL_VERSION_TABLE,
    SOURCE_TABLE,
    build_v2_registry,
    register_tables,
    table_definitions,
)

EXPECTED_TABLES = {
    MEMORY_TABLE,
    ENTITY_TABLE,
    SOURCE_TABLE,
    ADD_RECORD_TABLE,
    SCHEMA_ADD_BUFFER_TABLE,
    SEARCH_RECORD_TABLE,
    SKILL_VERSION_TABLE,
    SKILL_BLOB_TABLE,
    SKILL_TRACE_PENDING_TABLE,
    SKILL_TRACE_SUMMARY_TABLE,
}


def test_v2_defines_every_business_table_behind_one_of_the_three_ports() -> None:
    definitions = table_definitions(vector_dimensions=1536, sparse_hash_dim=2_000_000)

    assert {definition.name for definition in definitions} == EXPECTED_TABLES
    assert {definition.port for definition in definitions} == {"memory", "recorder", "skill"}
    assert [definition.port for definition in definitions].count("memory") == 3
    assert [definition.port for definition in definitions].count("recorder") == 3
    assert [definition.port for definition in definitions].count("skill") == 4


def test_v2_registry_is_frozen_and_preserves_vector_layout() -> None:
    registry = build_v2_registry(vector_dimensions=3072, sparse_hash_dim=1_000_003)

    assert {spec.name for spec in registry.specs} == EXPECTED_TABLES
    assert [(vector.name, vector.dimensions, vector.sparse) for vector in registry.get(MEMORY_TABLE).vectors] == [
        ("semantic", 3072, False),
        ("bm25", 1_000_003, True),
    ]
    assert [vector.name for vector in registry.get(ENTITY_TABLE).vectors] == ["semantic", "bm25"]
    assert [vector.name for vector in registry.get(SOURCE_TABLE).vectors] == ["semantic"]
    assert registry.get(ADD_RECORD_TABLE).vectors == ()

    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(registry.get(MEMORY_TABLE))


def test_base_registration_can_extend_a_composition_registry_without_a_second_catalog() -> None:
    definitions = table_definitions(vector_dimensions=3, sparse_hash_dim=8)
    registry = TableRegistry()

    register_tables(registry, definitions)

    assert {spec.name for spec in registry.specs} == EXPECTED_TABLES


def test_scope_is_an_envelope_not_duplicated_in_business_columns() -> None:
    registry = build_v2_registry(vector_dimensions=3, sparse_hash_dim=8)

    for spec in registry.specs:
        fields = {field.name for field in spec.fields}
        assert "project_id" not in fields
        assert "account_id" not in fields
        assert spec.scope_scoped is True
