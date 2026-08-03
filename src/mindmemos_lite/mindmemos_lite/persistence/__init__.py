"""Persistence composition helpers for configured vector-store backends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..config import DatabaseConfig
from ..infra.vector_store import (
    BackendConfig,
    BackendRegistry,
    BackendRequirements,
    GraphTableNames,
    TableRegistry,
    VectorDBService,
    with_graph_tables,
)
from .memory import MemoryPersistence
from .recorder import AddRecordPersistence, MemoryOperationRecorder, SearchRecordPersistence


def build_backend_config(config: DatabaseConfig) -> BackendConfig:
    """Translate the typed application config into the backend-neutral contract."""

    provider = config.backend.provider.strip().lower()
    if provider != "pgvector":
        raise ValueError(f"unsupported database backend {config.backend.provider!r}")

    pgvector = config.pgvector
    options: dict[str, Any] = {
        "dsn": pgvector.dsn,
        "schema": pgvector.schema,
        "min_pool_size": pgvector.min_pool_size,
        "max_pool_size": pgvector.max_pool_size,
        "pool_timeout": pgvector.pool_timeout,
        "create_extension": pgvector.create_extension,
        "create_schema": pgvector.create_schema,
        "hybrid_prefetch_factor": pgvector.hybrid_prefetch_factor,
        "rrf_k": pgvector.rrf_k,
        "dense_weight": pgvector.dense_weight,
        "sparse_weight": pgvector.sparse_weight,
    }
    required = config.backend.required
    return BackendConfig(
        provider=provider,
        options=options,
        required=BackendRequirements(
            dense_vector=required.dense_vector,
            sparse_vector=required.sparse_vector,
            hybrid_search=required.hybrid_search,
            metadata_filtering=required.metadata_filtering,
            batch_record_io=required.batch_record_io,
            atomic_batch_write=required.atomic_batch_write,
            max_vector_dimensions=required.max_vector_dimensions,
        ),
    )


def register_builtin_backends(registry: BackendRegistry) -> None:
    """Register backend implementations shipped with MindMemOS Lite."""

    from ..infra.vector_store.vector_store_impl import register_pgvector_backend

    register_pgvector_backend(registry)


def create_vector_db_service(
    config: DatabaseConfig,
    tables: TableRegistry,
    *,
    backends: BackendRegistry | None = None,
    graph_tables: GraphTableNames | None = None,
    node_tables: Mapping[str, str] | None = None,
) -> VectorDBService:
    """Compose the configured backend and the application-facing database service."""

    registry = backends
    if registry is None:
        registry = BackendRegistry()
        register_builtin_backends(registry)

    table_names = graph_tables or GraphTableNames()
    backend_tables = tables
    if config.backend.graph_enabled:
        backend_tables = with_graph_tables(tables, table_names=table_names)

    backend = registry.create(build_backend_config(config), backend_tables)
    return VectorDBService(
        backend,
        graph_enabled=config.backend.graph_enabled,
        graph_tables=table_names,
        node_tables=node_tables,
    )


async def ensure_database_schema(
    config: DatabaseConfig,
    tables: TableRegistry,
    *,
    backends: BackendRegistry | None = None,
    graph_tables: GraphTableNames | None = None,
    node_tables: Mapping[str, str] | None = None,
) -> VectorDBService:
    """Create the configured service and initialize its complete logical schema.

    The returned service owns the backend connection pool and must be closed by
    the caller. If initialization fails, this function closes it before
    propagating the original error.
    """

    service = create_vector_db_service(
        config,
        tables,
        backends=backends,
        graph_tables=graph_tables,
        node_tables=node_tables,
    )
    try:
        await service.ensure_schema(tables)
    except BaseException:
        await service.close()
        raise
    return service


__all__ = [
    "build_backend_config",
    "create_vector_db_service",
    "ensure_database_schema",
    "register_builtin_backends",
    "MemoryPersistence",
    "AddRecordPersistence",
    "MemoryOperationRecorder",
    "SearchRecordPersistence",
]
