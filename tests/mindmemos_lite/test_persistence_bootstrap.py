from __future__ import annotations

from typing import Any

import pytest
from mindmemos_lite.config import DatabaseConfig, PgVectorConfig
from mindmemos_lite.infra.vector_store import (
    BackendCapabilities,
    BackendRegistry,
    FieldSpec,
    FieldType,
    TableRegistry,
    TableSpec,
)
from mindmemos_lite.persistence import ensure_database_schema


class _BootstrapBackend:
    name = "pgvector"
    capabilities = BackendCapabilities(
        dense_vector=True,
        sparse_vector=True,
        hybrid_search=True,
        metadata_filtering=True,
        batch_record_io=True,
        atomic_batch_write=True,
        max_vector_dimensions=16_000,
    )

    def __init__(self) -> None:
        self.constructed_tables: TableRegistry | None = None
        self.ensured_tables: TableRegistry | None = None
        self.closed = False

    async def ensure_schema(self, tables: TableRegistry) -> None:
        self.ensured_tables = tables

    async def close(self) -> None:
        self.closed = True


def _business_tables() -> TableRegistry:
    tables = TableRegistry(
        (
            TableSpec(
                name="memory",
                primary_key="memory_id",
                fields=(FieldSpec(name="memory_id", field_type=FieldType.TEXT, nullable=False),),
            ),
        )
    )
    tables.freeze()
    return tables


@pytest.mark.asyncio
async def test_python_bootstrap_ensures_business_and_graph_schema() -> None:
    backend = _BootstrapBackend()
    registry = BackendRegistry()

    def factory(_options: Any, tables: TableRegistry):
        backend.constructed_tables = tables
        return backend

    registry.register("pgvector", factory)
    config = DatabaseConfig(
        pgvector=PgVectorConfig(dsn="postgresql://unused"),
    )

    service = await ensure_database_schema(
        config,
        _business_tables(),
        backends=registry,
    )

    try:
        expected_tables = {"memory", "graph_node", "graph_edge"}
        assert {table.name for table in backend.constructed_tables.specs} == expected_tables
        assert {table.name for table in backend.ensured_tables.specs} == expected_tables
        assert service.graph_enabled is True
    finally:
        await service.close()

    assert backend.closed is True


@pytest.mark.asyncio
async def test_python_bootstrap_closes_backend_when_schema_initialization_fails() -> None:
    class FailingBackend(_BootstrapBackend):
        async def ensure_schema(self, tables: TableRegistry) -> None:
            raise RuntimeError("schema failed")

    backend = FailingBackend()
    registry = BackendRegistry()
    registry.register("pgvector", lambda _options, _tables: backend)
    config = DatabaseConfig(
        pgvector=PgVectorConfig(dsn="postgresql://unused"),
    )

    with pytest.raises(RuntimeError, match="schema failed"):
        await ensure_database_schema(
            config,
            _business_tables(),
            backends=registry,
        )

    assert backend.closed is True
