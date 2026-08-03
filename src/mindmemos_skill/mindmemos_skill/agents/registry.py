"""Agent lookup helper wrapping the global component registry."""

from ..registry import create, list_components


def get_agent(name: str = "claude", **kwargs):
    """Create an agent instance by name.

    Args:
        name: Registered agent name (default ``"claude"``).
        **kwargs: Forwarded to the agent constructor.

    Returns:
        An :class:`~.base.Agent`-compatible instance.
    """
    return create(type="agent", name=name, **kwargs)


def list_agents() -> list[str]:
    """List names of all registered agent implementations."""
    return list_components(type="agent").get("agent", [])


__all__ = [
    "get_agent",
    "list_agents",
]
