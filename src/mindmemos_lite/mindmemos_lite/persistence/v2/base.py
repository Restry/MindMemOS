"""Small building blocks shared by the persistence-v2 table modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from ...infra.vector_store import FieldSpec, FieldType, TableRegistry, TableSpec

PersistencePortName = Literal["memory", "recorder", "skill"]


@dataclass(frozen=True, slots=True)
class TableDefinition:
    """Associate one backend-neutral table declaration with its persistence port."""

    port: PersistencePortName
    spec: TableSpec

    @property
    def name(self) -> str:
        return self.spec.name


def column(
    name: str,
    field_type: FieldType,
    *,
    nullable: bool = True,
    default: object = None,
) -> FieldSpec:
    """Declare one typed payload column without leaking a database driver type."""

    return FieldSpec(name=name, field_type=field_type, nullable=nullable, default=default)


def required(name: str, field_type: FieldType) -> FieldSpec:
    return column(name, field_type, nullable=False)


def schema_version_column() -> FieldSpec:
    return column("schema_version", FieldType.INTEGER, nullable=False, default=2)


def register_tables(registry: TableRegistry, definitions: Iterable[TableDefinition]) -> None:
    """Register definitions in an existing bootstrap registry."""

    for definition in definitions:
        registry.register(definition.spec)


def build_registry(definitions: Iterable[TableDefinition]) -> TableRegistry:
    """Build the single frozen registry consumed by vector-store backends."""

    registry = TableRegistry()
    register_tables(registry, definitions)
    registry.freeze()
    return registry


__all__ = [
    "TableDefinition",
    "PersistencePortName",
    "build_registry",
    "column",
    "register_tables",
    "required",
    "schema_version_column",
]
