"""In-memory Memory backend contract tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

schema = pytest.importorskip("mindmemos_lite.service.schema")

from mindmemos_sdk.config import DefaultsConfig, InMemoryConnectionConfig  # noqa: E402
from mindmemos_sdk.connections import InMemoryConnection  # noqa: E402
from mindmemos_sdk.memory import AsyncMemoryClient, InMemoryMemoryBackend  # noqa: E402
from mindmemos_sdk.memory.backends import in_memory as in_memory_backend_module  # noqa: E402


@pytest.mark.asyncio
async def test_in_memory_memory_backend_maps_all_operations() -> None:
    calls: list[tuple[str, object, object]] = []

    class Service:
        async def add(self, context, request):
            calls.append(("add", context, request))
            return schema.AddMemoryResult(
                status="ok",
                memories=(
                    schema.MemoryAddEvent(
                        operation="add",
                        content="hello",
                        memory_id="m1",
                        memory_type="fact",
                    ),
                ),
            )

        async def search(self, context, request):
            calls.append(("search", context, request))
            return self._list_result()

        async def get(self, context, request):
            calls.append(("get", context, request))
            return self._list_result()

        async def update(self, context, request):
            calls.append(("update", context, request))
            return schema.MemoryMutationResult(status="ok", message="updated")

        async def delete(self, context, request):
            calls.append(("delete", context, request))
            return schema.MemoryMutationResult(status="ok", message="deleted")

        async def feedback(self, context, request):
            calls.append(("feedback", context, request))
            return schema.FeedbackMemoryResult(status="ok", message="processed")

        async def dream(self, context, request):
            calls.append(("dreaming", context, request))
            return schema.MemoryMutationResult(status="queued", message="queued")

        @staticmethod
        def _list_result():
            return schema.MemoryListResult(
                status="ok",
                memories=(
                    schema.MemoryItem(
                        memory_id="m1",
                        content="hello",
                        updated_at=datetime(2026, 7, 27, tzinfo=UTC),
                        lineage=schema.MemoryLineage(
                            derived_from_memory_ids=("m0",),
                        ),
                    ),
                ),
            )

    class Runtime:
        is_running = True
        memory = Service()

    connection = InMemoryConnection(
        InMemoryConnectionConfig(
            project_id="project-1",
            account_id="account-1",
            api_key_uuid="key-1",
        ),
        runtime=Runtime(),
    )
    await connection.open()
    defaults = DefaultsConfig(
        user_id="alice",
        app_id="app-1",
        agent_id="agent-1",
        session_id="session-1",
    )
    client = AsyncMemoryClient(
        InMemoryMemoryBackend(connection, defaults=defaults),
        default_user_id=defaults.user_id,
        default_app_id=defaults.app_id,
        default_agent_id=defaults.agent_id,
        default_session_id=defaults.session_id,
    )

    added = await client.add([{"role": "user", "content": "hello"}])
    searched = await client.search("hello")
    fetched = await client.get()
    updated = await client.update("m1", "updated")
    deleted = await client.delete("m1")
    feedback = await client.feedback()
    dreamed = await client.dreaming()

    assert added.memories[0].mem_type == "fact"
    assert searched.memories[0].id == fetched.memories[0].id == "m1"
    assert searched.memories[0].last_update_at == "2026-07-27 00:00:00"
    assert searched.memories[0].lineage.derived_from_memory_ids == ["m0"]
    assert updated.message == "updated"
    assert deleted.message == "deleted"
    assert feedback.message == "processed"
    assert dreamed.code == "queued"
    assert [operation for operation, _, _ in calls] == [
        "add",
        "search",
        "get",
        "update",
        "delete",
        "feedback",
        "dreaming",
    ]
    contexts = [context for _, context, _ in calls]
    assert all(context.project_id == "project-1" for context in contexts)
    assert all(context.user_id == "alice" for context in contexts)
    assert all(context.app_id == "app-1" for context in contexts)
    assert all(context.agent_id == "agent-1" for context in contexts)
    assert all(context.session_id == "session-1" for context in contexts)
    assert len({context.request_id for context in contexts}) == len(contexts)

    await connection.aclose()


@pytest.mark.asyncio
async def test_in_memory_memory_backend_binds_project_override_config(monkeypatch) -> None:
    override = {
        "algo_config": {
            "add": {"vanilla": {"enable_entities": False}},
            "search": {"vanilla": {"use_reranker": True}},
        }
    }
    bound_configs: list[dict[str, object]] = []
    active_configs: list[dict[str, object]] = []

    @contextmanager
    def bind_config_overrides(*, project_config):
        bound_configs.append(project_config)
        active_configs.append(project_config)
        try:
            yield
        finally:
            active_configs.pop()

    monkeypatch.setattr(
        in_memory_backend_module,
        "_load_config_context",
        lambda: SimpleNamespace(bind_config_overrides=bind_config_overrides),
    )

    class Service:
        async def search(self, _context, _request):
            assert active_configs == [override]
            return schema.MemoryListResult(status="ok", memories=())

        add = get = update = delete = feedback = dream = search

    class Runtime:
        is_running = True
        memory = Service()

    connection = InMemoryConnection(
        InMemoryConnectionConfig(
            project_id="project-1",
            project_override_config=override,
        ),
        runtime=Runtime(),
    )
    await connection.open()
    client = AsyncMemoryClient(InMemoryMemoryBackend(connection))

    result = await client.search("hello", user_id="alice")

    assert result.memories == []
    assert bound_configs == [override]
    assert active_configs == []
    await connection.aclose()
