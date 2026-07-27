"""Backend contract implemented by vector/document database adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .models import BackendCapabilities, Record, RecordQuery, VectorHit, VectorQuery
from .scope import DatabaseScope

if TYPE_CHECKING:
    from .registry import TableRegistry


class ScopedVectorStore(ABC):
    """实现带基础权限隔离的Vector Store"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable provider name used by backend configuration."""

        ...

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Declare the effective record, filter, and vector features."""

        ...

    @abstractmethod
    async def ensure_schema(self, tables: TableRegistry) -> None:
        """Create or validate logical collections, fields, indexes, and vectors."""

        ...

    @abstractmethod
    async def upsert_records(self, table: str, records: Sequence[Record]) -> None:
        """Insert or replace records by their scoped logical identities."""

        ...

    @abstractmethod
    async def get_records(
        self,
        table: str,
        scope: DatabaseScope,
        record_ids: Sequence[str],
        *,
        with_vectors: bool = False,
    ) -> list[Record]:
        """Batch-read records whose IDs and stored scopes match the request."""

        ...

    @abstractmethod
    async def patch_record(
        self,
        table: str,
        scope: DatabaseScope,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> None:
        """Partially update one in-scope record."""

        ...

    @abstractmethod
    async def delete_records(self, table: str, scope: DatabaseScope, record_ids: Sequence[str]) -> None:
        """Idempotently delete only records matching the supplied scope and IDs."""

        ...

    @abstractmethod
    async def query_records(self, table: str, query: RecordQuery) -> tuple[list[Record], str | None]:
        """Filter, order, and page records through the portable record query AST."""

        ...

    @abstractmethod
    async def scroll(
        self,
        table: str,
        query: RecordQuery,
        *,
        with_vectors: bool = False,
    ) -> tuple[list[Record], str | None]:
        """Iterate records by filter/order/cursor without calculating vector scores.

        The cursor is backend-owned and must be passed unchanged to the next
        call with the same table and compatible query.  Implementations must
        apply the query scope before returning records and may include vectors
        only when ``with_vectors`` is true.
        """

        ...

    @abstractmethod
    async def search_vectors(self, query: VectorQuery) -> list[VectorHit]:
        """Run dense, sparse, or hybrid search and return backend-neutral hits."""

        ...

    @abstractmethod
    async def close(self) -> None:
        """Idempotently release connections and other backend-owned resources."""

        ...
