"""Contract implemented by structured persistence database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .models import DatabaseCapabilities, Record, RecordQuery
from .scope import DatabaseScope

if TYPE_CHECKING:
    from .registry import TableRegistry


class ScopedDatabase(ABC):
    """Scoped structured-record storage with no vector-search capability."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> DatabaseCapabilities: ...

    @abstractmethod
    async def ensure_schema(self, tables: TableRegistry) -> None: ...

    @abstractmethod
    async def upsert_records(self, table: str, records: Sequence[Record]) -> None: ...

    @abstractmethod
    async def get_records(
        self,
        table: str,
        scope: DatabaseScope,
        record_ids: Sequence[str],
    ) -> list[Record]: ...

    @abstractmethod
    async def patch_record(
        self,
        table: str,
        scope: DatabaseScope,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> None: ...

    @abstractmethod
    async def delete_records(self, table: str, scope: DatabaseScope, record_ids: Sequence[str]) -> None: ...

    @abstractmethod
    async def query_records(self, table: str, query: RecordQuery) -> tuple[list[Record], str | None]: ...

    @abstractmethod
    async def close(self) -> None: ...


__all__ = ["ScopedDatabase"]
