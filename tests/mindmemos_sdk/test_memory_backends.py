"""Contract tests for Memory clients and asynchronous backends."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mindmemos_sdk.client import MindMemOSClient
from mindmemos_sdk.config import ConfigManager, HttpConnectionConfig, SDKConfig
from mindmemos_sdk.connections import HttpConnection
from mindmemos_sdk.memory import AsyncMemoryBackend, AsyncMemoryClient, HttpMemoryBackend, MemoryClient
from mindmemos_sdk.memory.core import MemoryRequest
from mindmemos_sdk.transport import AsyncHttpTransport, Envelope, HttpTransport

EXPECTED_OPERATIONS = [
    "add",
    "search",
    "get",
    "update",
    "delete",
    "feedback",
    "dreaming",
]


def _envelope_for(operation: str) -> Envelope:
    if operation == "add":
        data = {"memories": [{"operation": "add", "content": "hello", "memory_id": "m1"}]}
    elif operation in {"search", "get"}:
        data = {"memories": [{"id": "m1", "memory": "hello"}]}
    else:
        data = None
    return Envelope(code="ok", message="done", request_id=f"req-{operation}", data=data)


class RecordingMemoryBackend(AsyncMemoryBackend):
    def __init__(self) -> None:
        self.operations: list[str] = []

    async def execute(self, request: MemoryRequest[Any]) -> Any:
        self.operations.append(request.operation)
        return request.parse(_envelope_for(request.operation))


def test_memory_backend_contract_is_abstract() -> None:
    with pytest.raises(TypeError):
        AsyncMemoryBackend()


@pytest.mark.asyncio
async def test_async_memory_client_accepts_backend_and_labels_all_operations() -> None:
    backend = RecordingMemoryBackend()
    client = AsyncMemoryClient(backend, default_user_id="u-1")

    await client.add([{"role": "user", "content": "hello"}])
    await client.search("hello")
    await client.get()
    await client.update("m1", "updated")
    await client.delete("m1")
    await client.feedback()
    await client.dreaming()

    assert backend.operations == EXPECTED_OPERATIONS


def test_sync_memory_client_executes_requests_through_http_transport() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        operation = request.url.path.rsplit("/", 1)[-1]
        envelope = _envelope_for(operation)
        return httpx.Response(200, json=envelope.model_dump(mode="json"))

    transport = HttpTransport(
        base_url="https://api.test",
        api_key="mk-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = MemoryClient(transport, default_user_id="u-1")

    client.add([{"role": "user", "content": "hello"}])
    client.search("hello")
    client.get()
    client.update("m1", "updated")
    client.delete("m1")
    client.feedback()
    client.dreaming()

    assert paths == [f"/v1/memory/{operation}" for operation in EXPECTED_OPERATIONS]


@pytest.mark.asyncio
async def test_http_memory_backend_uses_request_path_and_parser() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(200, json={"code": "ok", "data": None})

    transport = AsyncHttpTransport(
        base_url="https://api.test",
        api_key="mk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    connection = HttpConnection(
        HttpConnectionConfig(base_url="https://api.test"),
        transport=transport,
        owns_transport=True,
    )
    backend = HttpMemoryBackend(connection)
    request = MemoryRequest(
        operation="delete",
        path="/not-derived-from-operation",
        body={"memory_id": "m1"},
        parse=lambda envelope: envelope.code,
    )

    assert await backend.execute(request) == "ok"
    assert captured["path"] == "/not-derived-from-operation"
    assert captured["body"] == b'{"memory_id":"m1"}'
    await connection.aclose()


def test_root_client_does_not_close_external_transport(tmp_path) -> None:
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    transport = HttpTransport(
        base_url="https://api.test",
        api_key="mk-test",
        client=http_client,
    )
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    transport.close = close
    client = MindMemOSClient(
        config=SDKConfig(),
        config_manager=ConfigManager(config_dir=tmp_path),
        transport=transport,
    )

    client.close()

    assert close_calls == 0
    http_client.close()


def test_root_client_closes_its_owned_transport(tmp_path) -> None:
    client = MindMemOSClient(
        config=SDKConfig(),
        config_manager=ConfigManager(config_dir=tmp_path),
    )
    close_calls = 0
    owned_close = client._transport.close

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        owned_close()

    client._transport.close = close

    client.close()

    assert close_calls == 1
