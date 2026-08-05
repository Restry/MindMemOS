"""对外提供统一的组件注册能力, env, datasets等"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..agents.base import Agent
    from ..agents.config import AgentConfig
    from ..envs.base import BaseEnv
    from ..typing import AgentType, EnvConfig

ComponentType = str

_VALID_COMPONENT_TYPES = {"env", "dataset", "algo", "agent"}
_COMPONENT_REGISTRY: dict[ComponentType, dict[str, type[Any]]] = {}
_BUILTINS_LOADED = False
_BUILTIN_MODULES = (
    "..agents.claude",
    "..agents.react",
    "..envs.registered_envs",
    "..datasets.alfworld",
    "..datasets.livemath",
)


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


def get_agent(
    *,
    agent_type: AgentType | str,
    config: AgentConfig | Mapping[str, Any],
    **kwargs: Any,
) -> Agent[Any]:
    """Create a configured Agent through the unified component registry."""

    from ..agents.base import Agent
    from ..typing import AgentType

    try:
        normalized_type = AgentType(agent_type)
    except ValueError as exc:
        raise ValueError(f"Unknown agent type: {agent_type!r}") from exc
    return cast(Agent[Any], create(type="agent", name=normalized_type.value, config=config, **kwargs))


def list_agents() -> list[str]:
    """List Agent names registered in the unified component registry."""

    return list_components(type="agent").get("agent", [])


def get_env(
    *,
    name: str,
    config: EnvConfig | Mapping[str, Any],
    **kwargs: Any,
) -> BaseEnv[Any]:
    """Create an environment selected by a future trainer configuration."""

    from ..envs.base import BaseEnv

    return cast(BaseEnv[Any], create(type="env", name=name, config=config, **kwargs))


def list_envs() -> list[str]:
    """List built-in and package-external registered environment names."""

    return list_components(type="env").get("env", [])


def load_builtin_components() -> None:
    """Import built-in component modules so their decorators run."""

    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    # Import built-in components so their @register decorators fire.
    # Mark the registry loaded only after every import succeeds so a partial
    # import cannot permanently hide missing components.
    for module_name in _BUILTIN_MODULES:
        import_module(module_name, package=__package__)
    _BUILTINS_LOADED = True


__all__ = [
    "ComponentType",
    "create",
    "get_agent",
    "get_env",
    "list_agents",
    "list_components",
    "list_envs",
    "load_builtin_components",
    "register",
]
