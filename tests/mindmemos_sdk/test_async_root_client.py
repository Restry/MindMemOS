"""Root async SDK composition tests."""

from __future__ import annotations

import httpx
import pytest
from mindmemos_sdk.config import ConfigManager, HttpConnectionConfig, SDKConfig
from mindmemos_sdk.connections import HttpConnection
from mindmemos_sdk.connections.base import AsyncConnection
from mindmemos_sdk.transport import AsyncHttpTransport

from mindmemos_sdk import AsyncMindMemOSClient


@pytest.mark.asyncio
async def test_async_root_routes_resources_through_one_shared_connection(tmp_path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/memory/search":
            data = {"memories": [{"id": "m1", "memory": "hello"}]}
        else:
            data = None
        return httpx.Response(200, json={"code": "ok", "data": data})

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
        HttpConnectionConfig(base_url="https://api.test", api_key="mk-test"),
        transport=transport,
        owns_transport=True,
    )
    config = SDKConfig(
        defaults={"user_id": "alice"},
        connections={
            "shared": HttpConnectionConfig(base_url="https://api.test", api_key="mk-test"),
        },
        clients={
            "memory": {"connection": "shared"},
            "skills": {"connection": "shared"},
        },
    )
    client = AsyncMindMemOSClient(
        config=config,
        config_manager=ConfigManager(config_dir=tmp_path / "sdk"),
        connections={"shared": connection},
    )

    async with client:
        result = await client.memory.search("hello")
        assert result.memories[0].id == "m1"
        assert client.skills.local.list_local() == []

    assert calls == ["/v1/memory/search"]
    assert close_calls == 1


def test_async_root_rejects_unknown_client_connection(tmp_path) -> None:
    config = SDKConfig(
        connections={
            "cloud": HttpConnectionConfig(base_url="https://api.test", api_key="mk-test"),
        }
    )

    with pytest.raises(ValueError, match="unknown SDK connection"):
        AsyncMindMemOSClient(
            config=config,
            config_manager=ConfigManager(config_dir=tmp_path / "sdk"),
        )


@pytest.mark.asyncio
async def test_async_root_does_not_open_unused_connections(tmp_path) -> None:
    class UnusedConnection(AsyncConnection):
        @property
        def capabilities(self) -> frozenset[str]:
            return frozenset({"memory", "skills"})

        async def open(self) -> None:
            raise AssertionError("unused connection must not be opened")

        async def aclose(self) -> None:
            raise AssertionError("unused connection must not be closed")

    transport = AsyncHttpTransport(
        base_url="https://api.test",
        api_key="mk-test",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": "ok", "data": None})
            )
        ),
    )
    selected = HttpConnection(
        HttpConnectionConfig(base_url="https://api.test", api_key="mk-test"),
        transport=transport,
    )
    config = SDKConfig(
        defaults={"user_id": "alice"},
        connections={
            "selected": HttpConnectionConfig(base_url="https://api.test"),
            "unused": HttpConnectionConfig(base_url="https://unused.test"),
        },
        clients={
            "memory": {"connection": "selected"},
            "skills": {"connection": "selected"},
        },
    )
    client = AsyncMindMemOSClient(
        config=config,
        config_manager=ConfigManager(config_dir=tmp_path / "sdk"),
        connections={
            "selected": selected,
            "unused": UnusedConnection(),
        },
    )

    async with client:
        pass

    await transport.aclose()
