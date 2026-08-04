"""Application-facing facade over a configured vector-store backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import BackendCapabilities, Record, RecordQuery, VectorHit, VectorQuery
from .registry import TableRegistry
from .scope import DatabaseScope
from .vector_store import ScopedVectorStore


class VectorDBService:
    """Expose record and vector operations without leaking a concrete driver."""

    def __init__(self, backend: ScopedVectorStore) -> None:
        self._backend = backend

    @property
    def name(self) -> str:
        return self._backend.name

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._backend.capabilities

    async def ensure_schema(self, tables: TableRegistry) -> None:
        await self._backend.ensure_schema(tables)

    async def close(self) -> None:
        await self._backend.close()

    async def upsert_records(self, table: str, records: Sequence[Record]) -> None:
        await self._backend.upsert_records(table, records)

    async def get_records(
        self,
        table: str,
        scope: DatabaseScope,
        record_ids: Sequence[str],
        *,
        with_vectors: bool = False,
    ) -> list[Record]:
        return await self._backend.get_records(table, scope, record_ids, with_vectors=with_vectors)

    async def patch_record(
        self,
        table: str,
        scope: DatabaseScope,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> None:
        await self._backend.patch_record(table, scope, record_id, changes)

    async def delete_records(self, table: str, scope: DatabaseScope, record_ids: Sequence[str]) -> None:
        await self._backend.delete_records(table, scope, record_ids)

    async def query_records(self, table: str, query: RecordQuery) -> tuple[list[Record], str | None]:
        return await self._backend.query_records(table, query)

    async def scroll(
        self,
        table: str,
        query: RecordQuery,
        *,
        with_vectors: bool = False,
    ) -> tuple[list[Record], str | None]:
        return await self._backend.scroll(table, query, with_vectors=with_vectors)

    async def search_vectors(self, query: VectorQuery) -> list[VectorHit]:
        return await self._backend.search_vectors(query)


__all__ = ["VectorDBService"]
