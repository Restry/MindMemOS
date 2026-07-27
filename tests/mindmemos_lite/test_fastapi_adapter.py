from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pytest
from mindmemos.api.app import create_app
from mindmemos.api.auth import FileApiKeyProvider, ResolvedApiKey
from mindmemos.errors import AuthenticationError
from mindmemos.service.schema import (
    AddMemoryResult,
    MemoryAddEvent,
    MemoryItem,
    MemoryListResult,
    MemoryMutationResult,
)


class _StaticApiKeyProvider:
    def __init__(self, *, scopes: tuple[str, ...] = ("memory:read", "memory:write")) -> None:
        self._resolved = ResolvedApiKey(
            account_id="account-1",
            project_id="project-1",
            key_id="key-1",
            memory_algorithm="vanilla",
            scopes=scopes,
        )

    def resolve(self, api_key: str) -> ResolvedApiKey:
        assert api_key == "secret"
        return self._resolved


class _FakeMemoryService:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, context, request):
        self.calls.append(("add", context, request))
        return AddMemoryResult(
            status="ok",
            memories=(
                MemoryAddEvent(
                    operation="add",
                    content="stored memory",
                    memory_id="memory-1",
                    memory_type="fact",
                ),
            ),
        )

    async def search(self, context, request):
        self.calls.append(("search", context, request))
        return MemoryListResult(
            status="ok",
            memories=(
                MemoryItem(
                    memory_id="memory-1",
                    content="stored memory",
                    updated_at=datetime(2026, 7, 25, 12, 30, 0),
                ),
            ),
        )

    async def get(self, context, request):
        self.calls.append(("get", context, request))
        return MemoryListResult(status="ok")

    async def delete(self, context, request):
        self.calls.append(("delete", context, request))
        return MemoryMutationResult(status="ok")

    async def update(self, context, request):
        self.calls.append(("update", context, request))
        return MemoryMutationResult(status="ok")

    async def feedback(self, context, request):
        self.calls.append(("feedback", context, request))
        raise NotImplementedError("feedback is not part of Lite yet")

    async def dream(self, context, request):
        self.calls.append(("dream", context, request))
        raise NotImplementedError("dreaming is not part of Lite yet")


class _FakeRuntime:
    def __init__(self) -> None:
        self._memory = _FakeMemoryService()
        self.state = "new"
        self.started = 0
        self.closed = 0

    @property
    def memory(self) -> _FakeMemoryService:
        # The real runtime requires service access on its owner event loop.
        asyncio.get_running_loop()
        return self._memory

    async def start(self):
        self.started += 1
        self.state = "running"
        return self

    async def close(self):
        self.closed += 1
        self.state = "closed"


@pytest.mark.asyncio
async def test_fastapi_lifespan_delegates_to_runtime_and_routes_to_runtime_service() -> None:
    runtime = _FakeRuntime()
    app = create_app(
        runtime_factory=lambda: runtime,
        api_key_provider=_StaticApiKeyProvider(),
    )

    async with app.router.lifespan_context(app):
        assert runtime.started == 1
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")
            response = await client.post(
                "/v1/memory/add",
                headers={"Authorization": "Bearer secret"},
                json={
                    "user_id": "user-1",
                    "messages": [{"text": "remember this"}],
                    "infer": False,
                },
            )

    assert runtime.closed == 1
    assert health.json() == {"status": "ok", "runtime": "running"}
    assert response.status_code == 200
    assert response.json()["data"]["memories"][0] == {
        "operation": "add",
        "content": "stored memory",
        "memory_id": "memory-1",
        "memory_type": "fact",
        "confidence": None,
        "related_memory_ids": [],
        "graph_edge_count": 0,
    }
    operation, context, command = runtime._memory.calls[0]
    assert operation == "add"
    assert context.project_id == "project-1"
    assert context.user_id == "user-1"
    assert command.infer is False
    assert command.messages[0].text == "remember this"


@pytest.mark.asyncio
async def test_fastapi_search_preserves_response_shape_and_enforces_scopes() -> None:
    runtime = _FakeRuntime()
    app = create_app(
        runtime_factory=lambda: runtime,
        api_key_provider=_StaticApiKeyProvider(scopes=("memory:read",)),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            search = await client.post(
                "/v1/memory/search",
                headers={"Authorization": "Bearer secret"},
                json={"query": "stored"},
            )
            forbidden = await client.post(
                "/v1/memory/add",
                headers={"Authorization": "Bearer secret"},
                json={"messages": [{"text": "no write scope"}]},
            )
            unauthenticated = await client.post("/v1/memory/search", json={"query": "stored"})

    assert search.status_code == 200
    assert search.json()["data"]["memories"] == [
        {
            "id": "memory-1",
            "memory": "stored memory",
            "memory_type": "fact",
            "last_update_at": "2026-07-25 12:30:00",
            "event_time": None,
            "source_timestamp": None,
            "lineage": None,
        }
    ]
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "auth.insufficient_scope"
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "auth.missing_authorization"


@pytest.mark.asyncio
async def test_unmigrated_http_capability_returns_not_implemented() -> None:
    runtime = _FakeRuntime()
    app = create_app(
        runtime_factory=lambda: runtime,
        api_key_provider=_StaticApiKeyProvider(),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/memory/feedback",
                headers={"Authorization": "Bearer secret"},
                json={"feedback": "correct it"},
            )

    assert response.status_code == 501
    assert response.json()["code"] == "not_implemented"


def test_file_api_key_provider_loads_enabled_keys_only(tmp_path) -> None:
    path = tmp_path / "api_keys.yaml"
    path.write_text(
        """
api_keys:
  - key_id: enabled-key
    api_key: enabled-secret
    project_id: project-1
    memory_algorithm: vanilla
    enabled: true
    scopes: [memory:read]
  - key_id: disabled-key
    api_key: disabled-secret
    project_id: project-2
    enabled: false
""",
        encoding="utf-8",
    )

    provider = FileApiKeyProvider(path)

    assert provider.resolve("enabled-secret").project_id == "project-1"
    with pytest.raises(AuthenticationError, match="invalid API key"):
        provider.resolve("disabled-secret")
