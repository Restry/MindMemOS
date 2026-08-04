"""Backend-neutral isolation scope for structured persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias

ScopeValue: TypeAlias = str | int | float | bool


@dataclass(frozen=True, slots=True, init=False)
class DatabaseScope:
    """Exact-match dimensions applied by every database operation.

    The database layer deliberately does not prescribe business dimensions.
    Persistence may supply ``project_id``, ``run_id``, or an empty scope for a
    process-local database without teaching infra what those values mean.
    """

    _items: tuple[tuple[str, ScopeValue], ...]

    def __init__(
        self,
        initial: Mapping[str, ScopeValue | None] | None = None,
        /,
        **dimensions: ScopeValue | None,
    ) -> None:
        merged = dict(initial or {})
        for name, value in dimensions.items():
            if name in merged and merged[name] != value:
                raise ValueError(f"scope dimension {name!r} was supplied more than once with different values")
            merged[name] = value

        normalized: list[tuple[str, ScopeValue]] = []
        for name, value in merged.items():
            if not isinstance(name, str) or not name:
                raise ValueError("scope dimension names must be non-empty strings")
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"scope dimension {name!r} must be a scalar value")
            normalized.append((name, value))
        object.__setattr__(self, "_items", tuple(sorted(normalized)))

    @property
    def values(self) -> Mapping[str, ScopeValue]:
        return dict(self._items)

    def items(self) -> tuple[tuple[str, ScopeValue], ...]:
        return self._items

    def get(self, name: str, default: Any = None) -> ScopeValue | Any:
        return dict(self._items).get(name, default)

    def matches(self, candidate: "DatabaseScope") -> bool:
        candidate_values = dict(candidate._items)
        return all(candidate_values.get(name) == expected for name, expected in self._items)


__all__ = ["DatabaseScope", "ScopeValue"]
