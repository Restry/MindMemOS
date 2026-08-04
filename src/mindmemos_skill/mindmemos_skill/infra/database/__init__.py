"""Backend-neutral structured database capability used by persistence."""

from .backends import bootstrap_database, create_database, register_builtin_databases
from .database import ScopedDatabase
from .models import (
    ComparisonOperator,
    DatabaseCapabilities,
    DatabaseConfig,
    DatabaseRequirements,
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
    TableSpec,
)
from .registry import DatabaseFactory, DatabaseRegistry, TableRegistry
from .scope import DatabaseScope, ScopeValue

__all__ = [
    "ComparisonOperator",
    "DatabaseCapabilities",
    "DatabaseConfig",
    "DatabaseFactory",
    "DatabaseRegistry",
    "DatabaseRequirements",
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
    "ScopedDatabase",
    "Sort",
    "TableRegistry",
    "TableSpec",
    "bootstrap_database",
    "create_database",
    "register_builtin_databases",
]
