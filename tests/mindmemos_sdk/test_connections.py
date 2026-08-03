"""Connection routing and lifecycle tests for the async SDK core."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mindmemos_sdk.composition import ConnectionPool
from mindmemos_sdk.config import (
    HttpConnectionConfig,
    InMemoryConnectionConfig,
    SDKConfig,
)
from mindmemos_sdk.connections import AsyncConnection, HttpConnection, InMemoryConnection
from mindmemos_sdk.memory.backends import HttpMemoryBackend
from mindmemos_sdk.memory.core import MemoryRequest
from mindmemos_sdk.transport import AsyncHttpTransport


class RecordingConnection(AsyncConnection):
    def __init__(self, name: str, events: list[str], *, capabilities: frozenset[str]) -> None:
        self.name = name
        self.events = events
        self._capabilities = capabilities

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    async def open(self) -> None:
        self.events.append(f"open:{self.name}")

    async def aclose(self) -> None:
        self.events.append(f"close:{self.name}")


def test_legacy_sdk_config_resolves_implicit_http_connection() -> None:
    config = SDKConfig(
        base_url="https://api.test",
        auth={"api_key": "mk-test"},
        network={"timeout_seconds": 12, "max_retries": 4},
    )

    connections = config.resolved_connections()

    assert connections == {
        "default": HttpConnectionConfig(
            base_url="https://api.test",
            api_key="mk-test",
            timeout_seconds=12,
            max_retries=4,
        )
    }


def test_named_connections_and_per_client_routes_validate() -> None:
    config = SDKConfig.model_validate(
        {
            "connections": {
                "cloud": {
                    "type": "http",
                    "base_url": "https://api.test",
                    "api_key": "mk-test",
                },
                "embedded": {
                    "type": "in_memory",
                    "runtime": "mindmemos_lite",
                    "project_id": "project-1",
                },
            },
            "clients": {
                "memory": {"connection": "embedded"},
                "skills": {"connection": "cloud"},
            },
        }
    )

    assert isinstance(config.connections["cloud"], HttpConnectionConfig)
    assert isinstance(config.connections["embedded"], InMemoryConnectionConfig)
    assert config.clients.memory.connection == "embedded"
    assert config.clients.skills.connection == "cloud"


def test_in_memory_connection_uses_top_level_actor_defaults() -> None:
    config = SDKConfig.model_validate(
        {
            "defaults": {
                "user_id": "alice",
                "app_id": "app-1",
                "agent_id": "agent-1",
                "session_id": "session-1",
            },
            "connections": {
                "embedded": {
                    "type": "in_memory",
                    "project_id": "project-1",
                    # Legacy per-connection actor fields remain loadable but are ignored.
                    "user_id": "legacy-user",
                    "app_id": "legacy-app",
                }
            },
        }
    )

    connection = config.connections["embedded"]

    assert isinstance(connection, InMemoryConnectionConfig)
    assert {
        "user_id",
        "app_id",
        "agent_id",
        "session_id",
    }.isdisjoint(InMemoryConnectionConfig.model_fields)
    assert "user_id" not in connection.model_dump()
    assert "app_id" not in connection.model_dump()
    assert config.defaults.user_id == "alice"
    assert config.defaults.app_id == "app-1"
    assert config.defaults.agent_id == "agent-1"
    assert config.defaults.session_id == "session-1"


@pytest.mark.asyncio
async def test_connection_pool_opens_once_and_closes_in_reverse_order() -> None:
    events: list[str] = []
    pool = ConnectionPool(
        {
            "one": RecordingConnection("one", events, capabilities=frozenset({"memory"})),
            "two": RecordingConnection("two", events, capabilities=frozenset({"skills"})),
        }
    )

    await pool.open()
    await pool.open()
    assert pool.get("one", capability="memory").capabilities == frozenset({"memory"})
    with pytest.raises(ValueError, match="does not support"):
        pool.get("one", capability="skills")

    await pool.aclose()
    await pool.aclose()

    assert events == ["open:one", "open:two", "close:two", "close:one"]


@pytest.mark.asyncio
async def test_http_backend_borrows_connection_lifecycle() -> None:
    calls: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.content))
        return httpx.Response(200, json={"code": "ok", "data": None})

    transport = AsyncHttpTransport(
        base_url="https://api.test",
        api_key="mk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1

    transport.aclose = close
    connection = HttpConnection(
        HttpConnectionConfig(base_url="https://ignored.test"),
        transport=transport,
        owns_transport=True,
    )
    backend = HttpMemoryBackend(connection)
    request = MemoryRequest(
        operation="search",
        path="/v1/memory/search",
        body={"query": "hello"},
        parse=lambda envelope: envelope.code,
    )

    await connection.open()
    assert await backend.execute(request) == "ok"
    assert close_calls == 0
    await connection.aclose()

    assert calls == [("/v1/memory/search", b'{"query":"hello"}')]
    assert close_calls == 1


@pytest.mark.asyncio
async def test_in_memory_connection_borrows_running_runtime() -> None:
    class Runtime:
        is_running = True

        async def close(self) -> None:
            raise AssertionError("borrowed runtime must not be closed")

    runtime: Any = Runtime()
    connection = InMemoryConnection(
        InMemoryConnectionConfig(project_id="project-1"),
        runtime=runtime,
    )

    await connection.open()
    assert connection.runtime is runtime
    await connection.aclose()
