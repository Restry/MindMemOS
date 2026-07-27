"""Tests for the SDK SkillCloudClient protocol wrapper."""

from __future__ import annotations

import json

import httpx
import pytest
from mindmemos_sdk.skills import (
    EvolveCloudRequest,
    PromoteCloudRequest,
    PushVersionRequest,
    SkillCloudClient,
    SkillSyncRequestItem,
    SyncCloudItem,
    SyncCloudRequest,
)
from mindmemos_sdk.transport import HttpTransport


def _transport(handler) -> HttpTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpTransport(base_url="https://api.test", api_key="mk_test", client=client)


def _version(version_id: str = "v1") -> dict:
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
        "created_at": "2026-06-16T00:00:00Z",
        "future_field": "ignored",
    }


def test_register_sends_body_and_parses_result():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": "ok",
                "data": {
                    "cloud_skill_id": "cloud-1",
                    "version_id": "v1",
                    "version_label": "1.0.0",
                    "content_hash": "hash-1",
                    "status": "observed",
                    "extra": "ignored",
                },
            },
        )

    client = SkillCloudClient(_transport(handler))
    result = client.register(
        name="demo",
        content='[{"content":"x","path":"SKILL.md"}]',
        version_label="1.0.0",
        parent_version_id="parent-1",
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/v1/skills/register"
    assert captured["body"] == {
        "name": "demo",
        "content": '[{"content":"x","path":"SKILL.md"}]',
        "version_label": "1.0.0",
        "parent_version_id": "parent-1",
    }
    assert result.cloud_skill_id == "cloud-1"
    assert result.version_id == "v1"
    assert result.status.value == "observed"


def test_list_get_versions_and_content_use_expected_methods():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if str(request.url).endswith("/v1/skills"):
            data = {"skills": [{"cloud_skill_id": "cloud-1", "skill_name": "demo", "latest_version": _version()}]}
        elif str(request.url).endswith("/v1/skills/cloud-1/get"):
            data = {"cloud_skill_id": "cloud-1", "skill_name": "demo", "latest_version": _version()}
        elif "/versions?" in str(request.url):
            data = {"versions": [_version("v1"), _version("v2")]}
        else:
            data = {"version": _version("v1"), "content": '[{"content":"x","path":"SKILL.md"}]'}
        return httpx.Response(200, json={"code": "ok", "data": data})

    client = SkillCloudClient(_transport(handler))

    assert client.list_skills()[0].cloud_skill_id == "cloud-1"
    assert client.get_skill("cloud-1").skill_name == "demo"
    assert [version.version_id for version in client.versions_since("cloud-1", since="2026-06-16T00:00:00Z")] == [
        "v1",
        "v2",
    ]
    assert client.get_content("cloud-1", "v1").content == '[{"content":"x","path":"SKILL.md"}]'
    assert calls == [
        ("GET", "https://api.test/v1/skills"),
        ("POST", "https://api.test/v1/skills/cloud-1/get"),
        ("GET", "https://api.test/v1/skills/cloud-1/versions?since=2026-06-16T00%3A00%3A00Z"),
        ("GET", "https://api.test/v1/skills/cloud-1/versions/v1/content"),
    ]


def test_evolve_posts_cloud_skill_id_and_parses_result():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": "ok",
                "data": {
                    "cloud_skill_id": "cloud-1",
                    "evolved": True,
                    "pending_count": 8,
                    "threshold": 4,
                    "new_version_id": "v9",
                    "new_version_ids": ["v8", "v9"],
                    "summarized_count": 8,
                    "consumed_count": 8,
                    "extra": "ignored",
                },
            },
        )

    client = SkillCloudClient(_transport(handler))
    result = client.evolve("cloud-1")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/v1/skills/evolve"
    assert captured["body"] == {"cloud_skill_id": "cloud-1", "mode": "sync"}
    assert result.status == "ok"
    assert result.evolved is True
    assert result.new_version_id == "v9"
    assert result.new_version_ids == ["v8", "v9"]


def test_evolve_async_posts_mode_and_parses_queued_status():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": "queued",
                "data": {
                    "cloud_skill_id": "cloud-1",
                    "status": "queued",
                    "evolved": False,
                    "pending_count": 0,
                    "threshold": 0,
                },
            },
        )

    result = SkillCloudClient(_transport(handler)).evolve("cloud-1", mode="async")

    assert captured["body"] == {"cloud_skill_id": "cloud-1", "mode": "async"}
    assert result.status == "queued"
    assert result.evolved is False


def test_evolve_below_threshold_parses_shortfall():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "ok",
                "data": {"cloud_skill_id": "cloud-1", "evolved": False, "pending_count": 2, "threshold": 4},
            },
        )

    result = SkillCloudClient(_transport(handler)).evolve("cloud-1")
    assert result.evolved is False
    assert result.pending_count == 2
    assert result.new_version_id is None


def test_sync_sends_top_level_array_and_delete_uses_post():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content))
        if str(request.url).endswith("/v1/skills/sync"):
            assert json.loads(request.content) == [{"cloud_skill_id": "cloud-1", "local_version_id": "v1"}]
            return httpx.Response(
                200,
                json={
                    "code": "ok",
                    "data": {
                        "results": [
                            {
                                "cloud_skill_id": "cloud-1",
                                "local_version_id": "v1",
                                "has_update": True,
                                "published_head": _version("v2"),
                                "gating_status": "published",
                            }
                        ]
                    },
                },
            )
        if str(request.url).endswith("/v1/skills/cloud-1/delete"):
            return httpx.Response(200, json={"code": "ok", "data": None})
        return httpx.Response(200, json={"code": "ok", "data": None})

    client = SkillCloudClient(_transport(handler))

    result = client.sync([SkillSyncRequestItem(cloud_skill_id="cloud-1", local_version_id="v1")])
    client.delete_skill("cloud-1")

    assert result.results[0].has_update is True
    assert result.results[0].published_head.version_id == "v2"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://api.test/v1/skills/sync"
    assert calls[1] == ("POST", "https://api.test/v1/skills/cloud-1/delete", b"")


def test_push_version_uses_client_uuid_and_excludes_all_private_local_fields():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": "ok",
                "data": {
                    "cloud_skill_id": "00000000-0000-4000-8000-000000000010",
                    "version_id": "00000000-0000-4000-8000-000000000011",
                    "content_hash": "a" * 64,
                    "status": "observed",
                    "created_at": "2026-07-25T00:00:00Z",
                    "received_at": "2026-07-25T00:00:01Z",
                },
            },
        )

    request = PushVersionRequest(
        operation_id="00000000-0000-4000-8000-000000000012",
        name="demo",
        version_id="00000000-0000-4000-8000-000000000011",
        content='[{"content":"demo","path":"SKILL.md"}]',
        expected_content_hash="a" * 64,
        commit_message="Initial version",
        created_at="2026-07-25T00:00:00Z",
    )
    result = SkillCloudClient(_transport(handler)).push_version(request)

    assert result.version_id == request.version_id
    assert captured["body"]["version_id"] == request.version_id
    assert captured["body"]["expected_content_hash"] == "a" * 64
    for forbidden in ("linked_files", "local_snapshot_hash", "active_version_id", "published_head_id", "path"):
        assert forbidden not in captured["body"]

    with pytest.raises(Exception):
        PushVersionRequest.model_validate({**request.model_dump(), "linked_files": {"private.md": "secret"}})


def test_target_sync_evolve_and_promote_protocols_are_strict():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((str(request.url), body))
        if str(request.url).endswith("/sync"):
            return httpx.Response(
                200,
                json={
                    "code": "ok",
                    "data": {
                        "items": [
                            {
                                "cloud_skill_id": "cloud-1",
                                "versions": [],
                                "published_head_id": "v2",
                                "cloud_revision": 3,
                            }
                        ]
                    },
                },
            )
        if str(request.url).endswith("/evolve"):
            return httpx.Response(
                200,
                json={
                    "code": "queued",
                    "data": {
                        "cloud_skill_id": "cloud-1",
                        "status": "queued",
                        "evolved": False,
                        "pending_count": 0,
                        "threshold": 0,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": "ok",
                "data": {
                    "cloud_skill_id": "cloud-1",
                    "published_head_id": "v2",
                    "cloud_revision": 4,
                    "updated_at": "2026-07-25T00:00:00Z",
                },
            },
        )

    client = SkillCloudClient(_transport(handler))
    sync = client.sync_cloud(
        SyncCloudRequest(
            items=[
                SyncCloudItem(
                    cloud_skill_id="cloud-1",
                    known_version_ids=["v1"],
                    known_published_head_id="v1",
                    known_cloud_revision=2,
                )
            ]
        )
    )
    evolved = client.evolve_cloud(
        EvolveCloudRequest(
            operation_id="op-evolve",
            cloud_skill_id="cloud-1",
            base_version_id="v1",
            mode="async",
        )
    )
    promoted = client.promote(
        "cloud-1",
        PromoteCloudRequest(
            operation_id="op-promote",
            version_id="v2",
            expected_cloud_revision=3,
        ),
    )

    assert sync.items[0].published_head_id == "v2"
    assert evolved.status == "queued"
    assert promoted.cloud_revision == 4
    assert calls[0][1] == {
        "items": [
            {
                "cloud_skill_id": "cloud-1",
                "known_version_ids": ["v1"],
                "known_published_head_id": "v1",
                "known_cloud_revision": 2,
            }
        ]
    }
    assert calls[1][1] == {
        "operation_id": "op-evolve",
        "cloud_skill_id": "cloud-1",
        "base_version_id": "v1",
        "mode": "async",
    }
    assert calls[2][1] == {
        "operation_id": "op-promote",
        "version_id": "v2",
        "expected_cloud_revision": 3,
    }
