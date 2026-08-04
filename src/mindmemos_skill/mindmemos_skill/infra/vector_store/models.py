"""Backend-neutral storage structures.

These types preserve the semantics currently consumed by search and dreaming
without carrying Qdrant models, Cypher fragments, SQL expressions, or driver
objects across the database boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Mapping, TypeAlias

from .scope import DatabaseScope

JsonObject = dict[str, Any]

ComparisonOperator = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
    "icontains",
    "is_empty",
    "is_null",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class Predicate:
    field: str
    op: ComparisonOperator
    value: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FilterGroup:
    operator: Literal["and", "or", "not"] = "and"
    clauses: tuple["FilterExpression", ...] = field(default_factory=tuple)


FilterExpression: TypeAlias = Predicate | FilterGroup


def scope_predicates(scope: DatabaseScope) -> tuple[Predicate, ...]:
    """Translate all non-null scope values into exact-match predicates."""

    return tuple(Predicate(field=field_name, op="eq", value=value) for field_name, value in scope.items())


@dataclass(frozen=True, slots=True, kw_only=True)
class Sort:
    field: str
    direction: Literal["asc", "desc"] = "asc"


@dataclass(frozen=True, slots=True, kw_only=True)
class Page:
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("page limit must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordQuery:
    scope: DatabaseScope
    filters: FilterExpression | None = None
    sort: tuple[Sort, ...] = field(default_factory=tuple)
    page: Page = field(default_factory=Page)


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorQuery:
    table: str
    scope: DatabaseScope
    vector_name: str
    dense_vector: tuple[float, ...] | None = None
    sparse_indices: tuple[int, ...] | None = None
    sparse_values: tuple[float, ...] | None = None
    mode: Literal["dense", "sparse", "hybrid"] = "dense"
    filters: FilterExpression | None = None
    top_k: int = 10
    dense_limit: int | None = None
    sparse_limit: int | None = None
    score_threshold: float | None = None

    def __post_init__(self) -> None:
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")
        if self.dense_limit is not None and self.dense_limit <= 0:
            raise ValueError("dense_limit must be positive when provided")
        if self.sparse_limit is not None and self.sparse_limit <= 0:
            raise ValueError("sparse_limit must be positive when provided")
        if self.mode in {"dense", "hybrid"} and self.dense_vector is None:
            raise ValueError(f"{self.mode} search requires a dense vector")
        if self.mode in {"sparse", "hybrid"}:
            if self.sparse_indices is None or self.sparse_values is None:
                raise ValueError(f"{self.mode} search requires sparse indices and values")
            if len(self.sparse_indices) != len(self.sparse_values):
                raise ValueError("sparse query indices and values must have the same length")


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendCapabilities:
    """Features offered by one vector/document backend instance.

    The backend advertises only record, filter, and vector operations.
    """

    dense_vector: bool = True
    sparse_vector: bool = False
    hybrid_search: bool = False
    metadata_filtering: bool = True
    batch_record_io: bool = True
    atomic_batch_write: bool = False
    max_vector_dimensions: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendRequirements:
    """Features required by one application/backend configuration.

    Requirements use false defaults so selecting a backend without an
    explicit capability profile does not accidentally require every optional
    vector feature.
    """

    dense_vector: bool = False
    sparse_vector: bool = False
    hybrid_search: bool = False
    metadata_filtering: bool = False
    batch_record_io: bool = False
    atomic_batch_write: bool = False
    max_vector_dimensions: int | None = None

    def missing_from(self, available: BackendCapabilities) -> tuple[str, ...]:
        missing: list[str] = []
        for field_name in (
            "dense_vector",
            "sparse_vector",
            "hybrid_search",
            "metadata_filtering",
            "batch_record_io",
            "atomic_batch_write",
        ):
            if getattr(self, field_name) and not getattr(available, field_name):
                missing.append(field_name)
        if self.max_vector_dimensions is not None and (
            available.max_vector_dimensions is None or available.max_vector_dimensions < self.max_vector_dimensions
        ):
            missing.append(f"vector_dimensions>={self.max_vector_dimensions}")
        return tuple(missing)


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfig:
    provider: str
    options: Mapping[str, Any] = field(default_factory=dict)
    required: BackendRequirements = field(default_factory=BackendRequirements)


@dataclass(frozen=True, slots=True, kw_only=True)
class SparseVector:
    """Sparse vector independent of a vector database SDK."""

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError("sparse vector indices and values must have the same length")
        if any(index < 0 for index in self.indices):
            raise ValueError("sparse vector indices must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorValue:
    """Named dense/sparse vectors attached to one logical record."""

    dense: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    sparse: Mapping[str, SparseVector] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """One row/document/point in a logical table."""

    table: str
    record_id: str
    scope: DatabaseScope
    payload: Mapping[str, Any]
    vectors: VectorValue | None = None

    def __post_init__(self) -> None:
        if not self.table:
            raise ValueError("record table must not be empty")
        if not self.record_id:
            raise ValueError("record_id must not be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorHit:
    """Backend-neutral vector or hybrid-search result."""

    record: Record
    score: float
    source: str
    debug: Mapping[str, Any] = field(default_factory=dict)


class FieldType(StrEnum):
    UUID = "uuid"
    TEXT = "text"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    TEXT_ARRAY = "text_array"
    UUID_ARRAY = "uuid_array"
    JSON = "json"


class IndexKind(StrEnum):
    BTREE = "btree"
    FULL_TEXT = "full_text"


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSpec:
    name: str
    field_type: FieldType
    nullable: bool = True
    default: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexSpec:
    name: str
    fields: tuple[str, ...]
    unique: bool = False
    kind: IndexKind = IndexKind.BTREE

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("an index requires at least one field")


@dataclass(frozen=True, slots=True, kw_only=True)
class VectorFieldSpec:
    name: str
    dimensions: int
    distance: Literal["cosine", "euclidean", "dot"] = "cosine"
    sparse: bool = False

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            raise ValueError("vector dimensions must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class TableSpec:
    """One logical table; adapters decide its physical representation.

    For ``scope_scoped`` tables, ``Record.scope`` is an envelope outside the
    declared payload fields. Adapters must apply primary-key and unique-index
    semantics inside that scope and may store its arbitrary dimensions as
    JSON, metadata paths, typed columns, or another native representation.
    """

    name: str
    primary_key: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    indexes: tuple[IndexSpec, ...] = field(default_factory=tuple)
    vectors: tuple[VectorFieldSpec, ...] = field(default_factory=tuple)
    scope_scoped: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.primary_key:
            raise ValueError("table name and primary key must not be empty")
        field_names = {spec.name for spec in self.fields}
        if len(field_names) != len(self.fields):
            raise ValueError(f"table {self.name!r} contains duplicate fields")
        vector_names = {spec.name for spec in self.vectors}
        if len(vector_names) != len(self.vectors):
            raise ValueError(f"table {self.name!r} contains duplicate vector fields")
        index_names = [index.name for index in self.indexes]
        duplicate_indexes = sorted({name for name in index_names if index_names.count(name) > 1})
        if duplicate_indexes:
            raise ValueError(f"table {self.name!r} contains duplicate indexes: {duplicate_indexes}")
        for index in self.indexes:
            unknown = set(index.fields) - field_names - {self.primary_key}
            if unknown:
                raise ValueError(f"index {index.name!r} references unknown fields: {sorted(unknown)}")
