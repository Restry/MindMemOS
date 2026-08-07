"""Build SDK connections and resource backends from configuration."""

from __future__ import annotations

from ..config import DefaultsConfig, HttpConnectionConfig, SDKConfig
from ..connections import AsyncConnection, HttpConnection
from ..memory.backends import AsyncMemoryBackend, HttpMemoryBackend
from ..skills.backends import AsyncSkillBackend, HttpSkillBackend


def build_connections(config: SDKConfig) -> dict[str, AsyncConnection]:
    """Construct named connections without opening them."""

    connections: dict[str, AsyncConnection] = {}
    for name, connection_config in config.resolved_connections().items():
        if isinstance(connection_config, HttpConnectionConfig):
            connections[name] = HttpConnection(connection_config)
        else:  # pragma: no cover - protected by the discriminated config union
            raise TypeError(f"unsupported SDK connection config: {type(connection_config).__name__}")
    return connections


def build_memory_backend(
    connection: AsyncConnection,
    *,
    defaults: DefaultsConfig | None = None,
) -> AsyncMemoryBackend:
    if isinstance(connection, HttpConnection):
        return HttpMemoryBackend(connection)
    raise TypeError(f"connection does not provide a Memory backend: {type(connection).__name__}")


def build_skill_backend(
    connection: AsyncConnection,
    *,
    defaults: DefaultsConfig | None = None,
) -> AsyncSkillBackend:
    if isinstance(connection, HttpConnection):
        return HttpSkillBackend(connection)
    raise TypeError(f"connection does not provide a Skill backend: {type(connection).__name__}")


__all__ = ["build_connections", "build_memory_backend", "build_skill_backend"]
