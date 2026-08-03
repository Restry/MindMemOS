"""对外提供统一的组件注册能力, env, datasets等"""

from __future__ import annotations

from importlib import import_module
from typing import Any

ComponentType = str

_VALID_COMPONENT_TYPES = {"env", "dataset", "algo", "agent"}
_COMPONENT_REGISTRY: dict[ComponentType, dict[str, type[Any]]] = {}
_BUILTINS_LOADED = False


def register(*, type: ComponentType, name: str):
    """Register a component class under a type/name pair."""

    if type not in _VALID_COMPONENT_TYPES:
        valid = ", ".join(sorted(_VALID_COMPONENT_TYPES))
        raise ValueError(f"Unknown component type {type!r}. Valid types: {valid}")
    if not name:
        raise ValueError("component name must not be empty")

    def decorator(cls: type[Any]) -> type[Any]:
        components = _COMPONENT_REGISTRY.setdefault(type, {})
        if name in components:
            raise ValueError(f"{type} component {name!r} is already registered")
        components[name] = cls
        return cls

    return decorator


def create(*, type: ComponentType, name: str, **kwargs: Any) -> Any:
    """Create a registered component by type/name."""

    load_builtin_components()
    component_cls = _COMPONENT_REGISTRY.get(type, {}).get(name)
    if component_cls is None:
        available = ", ".join(sorted(_COMPONENT_REGISTRY.get(type, {}))) or "<none>"
        raise ValueError(f"Unknown {type} component {name!r}. Available {type} components: {available}")
    return component_cls(**kwargs)


def list_components(*, type: ComponentType | None = None) -> dict[str, list[str]]:
    """List all registered components, optionally filtered by type."""

    load_builtin_components()
    if type is not None:
        return {type: sorted(_COMPONENT_REGISTRY.get(type, {}))}
    return {t: sorted(names) for t, names in _COMPONENT_REGISTRY.items()}


def load_builtin_components() -> None:
    """Import built-in component modules so their decorators run."""

    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    _BUILTINS_LOADED = True

    # Import built-in components so their @register decorators fire.
    # Each module is importable independently; lazy imports avoid circular deps.
    try:
        from ..agents import claude  # noqa: F401
    except ImportError:
        pass
    try:
        from ..agents import claude_sdk  # noqa: F401
    except ImportError:
        pass
    try:
        from ..components.envs import builtin  # noqa: F401
    except ImportError:
        pass
    try:
        from ..components.datasets import builtin  # noqa: F401
    except ImportError:
        pass