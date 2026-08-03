"""Backend-parity tests for SDK Skill clients."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.skills import (
    AsyncSkillClient,
    SkillManager,
    SkillSyncRequestItem,
)
from mindmemos_sdk.transport import AsyncHttpTransport


def _version(version_id: str = "v1") -> dict[str, object]:
    return {
        "version_id": version_id,
        "project_id": "project-1",
        "cloud_skill_id": "cloud-1",
        "skill_name": "demo",
        "content_hash": f"hash-{version_id}",
        "parent_version_id": None,
        "version_label": "1.0.0",
        "status": "published",
        "origin": "edge",
        "created_at": "2026-07-27T00:00:00Z",
    }


def test_skill_manager_keeps_local_versions_without_api_backend(tmp_path) -> None:
    manager = SkillManager.from_config_manager(ConfigManager(config_dir=tmp_path / "sdk"))

    assert manager.list_local() == []
    with pytest.raises(Exception, match="no MindMemOS API backend"):
        manager._cloud_client()


@pytest.mark.asyncio
async def test_async_http_skill_backend_uses_mindmemos_contract(tmp_path) -> None:
    calls: list[tuple[str, str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body))
        path = request.url.path
        if path.endswith("/register"):
            data = {
                "cloud_skill_id": "cloud-1",
                "version_id": "v1",
                "version_label": "1.0.0",
                "content_hash": "hash-v1",
                "status": "published",
            }
        elif path == "/v1/skills":
            data = {
                "skills": [
                    {
                        "cloud_skill_id": "cloud-1",
                        "skill_name": "demo",
                        "latest_version": _version(),
                    }
                ]
            }
        elif path.endswith("/get"):
            data = {
                "cloud_skill_id": "cloud-1",
                "skill_name": "demo",
                "latest_version": _version(),
            }
        elif path.endswith("/versions"):
            data = {"versions": [_version()]}
        elif path.endswith("/content"):
            data = {"version": _version(), "content": "skill-content"}
        elif path.endswith("/evolve"):
            data = {
                "cloud_skill_id": "cloud-1",
                "evolved": False,
                "pending_count": 1,
                "threshold": 2,
            }
        elif path.endswith("/sync"):
            data = {
                "results": [
                    {
                        "cloud_skill_id": "cloud-1",
                        "local_version_id": "v1",
                        "has_update": False,
                        "gating_status": "published",
                    }
                ]
            }
        else:
            data = None
        return httpx.Response(200, json={"code": "ok", "data": data})

    transport = AsyncHttpTransport(
        base_url="https://api.test",
        api_key="mk_test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = AsyncSkillClient.from_http(
        transport,
        config_manager=ConfigManager(config_dir=tmp_path / "sdk"),
        owns_transport=True,
    )

    registered = await client.register(name="demo", content="skill-content", version_label="1.0.0")
    listed = await client.list_skills()
    detail = await client.get_skill("cloud-1")
    versions = await client.versions_since("cloud-1", since="2026-07-27T00:00:00Z")
    content = await client.get_content("cloud-1", "v1")
    evolved = await client.evolve("cloud-1")
    synced = await client.sync([SkillSyncRequestItem(cloud_skill_id="cloud-1", local_version_id="v1")])
    await client.delete_skill("cloud-1")
    await client.aclose()

    assert registered.version_id == "v1"
    assert listed[0].skill_name == detail.skill_name == "demo"
    assert versions[0].version_id == "v1"
    assert content.content == "skill-content"
    assert evolved.pending_count == 1
    assert synced.results[0].has_update is False
    assert calls[0] == (
        "POST",
        "https://api.test/v1/skills/register",
        {"name": "demo", "content": "skill-content", "version_label": "1.0.0"},
    )
    assert calls[-1] == ("POST", "https://api.test/v1/skills/cloud-1/delete", None)


@pytest.mark.asyncio
async def test_in_memory_skill_backend_maps_transport_neutral_service(tmp_path) -> None:
    schema = pytest.importorskip("mindmemos_lite.service.schema")
    seen_contexts = []

    def version(version_id: str = "v1"):
        return schema.SkillVersion(
            version_id=version_id,
            project_id="project-1",
            cloud_skill_id="cloud-1",
            skill_name="demo",
            content_hash=f"hash-{version_id}",
            status="published",
            origin="edge",
            created_at=datetime(2026, 7, 27, tzinfo=UTC),
            version_label="1.0.0",
        )

    class FakeLiteSkillService:
        async def register(self, context, request):
            seen_contexts.append(context)
            assert request.name == "demo"
            return schema.RegisterSkillResult(
                cloud_skill_id="cloud-1",
                version_id="v1",
                content_hash="hash-v1",
                status="published",
                version_label=request.version_label,
            )

        async def list_skills(self, context):
            seen_contexts.append(context)
            return (
                schema.SkillSummary(
                    cloud_skill_id="cloud-1",
                    skill_name="demo",
                    latest_version=version(),
                ),
            )

        async def get_skill(self, context, cloud_skill_id):
            seen_contexts.append(context)
            return schema.SkillSummary(
                cloud_skill_id=cloud_skill_id,
                skill_name="demo",
                latest_version=version(),
            )

        async def list_versions(self, context, cloud_skill_id, *, since=None):
            seen_contexts.append(context)
            assert since == datetime(2026, 7, 27, tzinfo=UTC)
            return (version(),)

        async def get_version_content(self, context, cloud_skill_id, version_id):
            seen_contexts.append(context)
            return schema.SkillContent(version=version(version_id), content="skill-content")

        async def evolve(self, context, request):
            seen_contexts.append(context)
            return schema.SkillEvolveResult(
                cloud_skill_id=request.cloud_skill_id,
                evolved=True,
                pending_count=2,
                threshold=2,
                new_version_id="v2",
                new_version_ids=("v2",),
            )

        async def sync(self, context, request):
            seen_contexts.append(context)
            return tuple(
                schema.SkillSyncResult(
                    cloud_skill_id=item.cloud_skill_id,
                    local_version_id=item.local_version_id,
                    has_update=False,
                    gating_status="published",
                )
                for item in request.items
            )

        async def delete_skill(self, context, cloud_skill_id):
            seen_contexts.append(context)

    client = AsyncSkillClient.from_lite_service(
        FakeLiteSkillService(),
        project_id="project-1",
        account_id="local-account",
        api_key_uuid="local-key",
        user_id="alice",
        app_id="app-1",
        agent_id="agent-1",
        session_id="session-1",
        config_manager=ConfigManager(config_dir=tmp_path / "sdk"),
    )

    assert client.local.list_local() == []
    assert (await client.register(name="demo", content="skill-content", version_label="1.0.0")).version_id == "v1"
    assert (await client.list_skills())[0].skill_name == "demo"
    assert (await client.get_skill("cloud-1")).cloud_skill_id == "cloud-1"
    assert (await client.versions_since("cloud-1", since="2026-07-27T00:00:00Z"))[0].version_id == "v1"
    assert (await client.get_content("cloud-1", "v1")).content == "skill-content"
    assert (await client.evolve("cloud-1")).new_version_ids == ["v2"]
    assert (await client.sync([SkillSyncRequestItem(cloud_skill_id="cloud-1", local_version_id="v1")])).results[
        0
    ].has_update is False
    await client.delete_skill("cloud-1")
    await client.aclose()

    class FakeRunningRuntime:
        is_running = True
        skill = FakeLiteSkillService()

    runtime_client = AsyncSkillClient.from_lite_runtime(
        FakeRunningRuntime(),
        project_id="project-1",
        account_id="local-account",
        api_key_uuid="local-key",
        config_manager=ConfigManager(config_dir=tmp_path / "runtime-sdk"),
    )
    assert (await runtime_client.list_skills())[0].skill_name == "demo"

    assert len(seen_contexts) == 9
    assert all(context.project_id == "project-1" for context in seen_contexts)
    assert all(context.account_id == "local-account" for context in seen_contexts)
    assert all(context.user_id == "alice" for context in seen_contexts[:8])
    assert all(context.app_id == "app-1" for context in seen_contexts[:8])
    assert all(context.agent_id == "agent-1" for context in seen_contexts[:8])
    assert all(context.session_id == "session-1" for context in seen_contexts[:8])
    assert len({context.request_id for context in seen_contexts}) == len(seen_contexts)
