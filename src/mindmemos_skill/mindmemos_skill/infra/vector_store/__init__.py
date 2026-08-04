"""Backend-neutral record and vector storage."""

from .backends import bootstrap_vector_store, create_vector_store, register_builtin_vector_stores
from .models import (
    BackendCapabilities,
    BackendConfig,
    BackendRequirements,
    FieldSpec,
    FieldType,
    FilterExpression,
    FilterGroup,
    IndexKind,
    IndexSpec,
    Page,
    Predicate,
    Record,
    RecordQuery,
    Sort,
    SparseVector,
    TableSpec,
    VectorFieldSpec,
    VectorHit,
    VectorQuery,
    VectorValue,
    scope_predicates,
)
from .registry import BackendRegistry, TableRegistry
from .scope import DatabaseScope, ScopeValue
from .service import VectorDBService
from .vector_store import ScopedVectorStore

__all__ = [
    "BackendCapabilities",
    "BackendConfig",
    "BackendRegistry",
    "BackendRequirements",
    "DatabaseScope",
    "FieldSpec",
    "FieldType",
    "FilterExpression",
    "FilterGroup",
    "IndexKind",
    "IndexSpec",
    "Page",
    "Predicate",
    "Record",
    "RecordQuery",
    "ScopeValue",
    "ScopedVectorStore",
    "Sort",
    "SparseVector",
    "TableRegistry",
    "TableSpec",
    "VectorDBService",
    "VectorFieldSpec",
    "VectorHit",
    "VectorQuery",
    "VectorValue",
    "bootstrap_vector_store",
    "create_vector_store",
    "register_builtin_vector_stores",
    "scope_predicates",
]
