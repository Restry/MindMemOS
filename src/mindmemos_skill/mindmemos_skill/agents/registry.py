"""Agent lookup helper wrapping the global component registry."""

from collections.abc import Mapping
from typing import Any, cast

from ..registry import create, list_components
from ..typing import AgentType
from .base import Agent
from .config import AgentConfig


def get_agent(
    *,
    agent_type: AgentType | str,
    config: AgentConfig | Mapping[str, Any],
) -> Agent[Any]:
    """Create a configured agent for ``agent_type``.

    Args:
        agent_type: Domain-level type of the requested agent implementation.
        config: Common and implementation-specific construction settings.

    Returns:
        A validated, configured :class:`~.base.Agent` instance.
    """
    try:
        normalized_type = AgentType(agent_type)
    except ValueError as exc:
        raise ValueError(f"Unknown agent type: {agent_type!r}") from exc

    return cast(Agent[Any], create(type="agent", name=normalized_type.value, config=config))


def list_agents() -> list[str]:
    """List registered agent type values."""
    return list_components(type="agent").get("agent", [])


__all__ = [
    "get_agent",
    "list_agents",
]
