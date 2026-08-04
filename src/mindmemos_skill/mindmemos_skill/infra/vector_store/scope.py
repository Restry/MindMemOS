from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias

ScopeValue: TypeAlias = str | int | float | bool


@dataclass(frozen=True, slots=True, init=False)
class DatabaseScope:
    """Backend-neutral exact-match dimensions for data isolation.

    db_lite does not know application field names. Callers supply any scalar
    dimensions their persistence policy requires. ``None`` values are omitted,
    which also lets query scopes intentionally address a broader namespace.
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

    def missing_required_fields(
        self,
        required_fields: frozenset[str],
    ) -> tuple[str, ...]:
        """Let an application policy validate its own required dimensions."""

        available = dict(self._items)
        return tuple(sorted(name for name in required_fields if name not in available))

    def matches(self, candidate: "DatabaseScope") -> bool:
        """Return whether ``candidate`` contains every dimension in this scope."""

        candidate_values = dict(candidate._items)
        return all(candidate_values.get(name) == expected for name, expected in self._items)
