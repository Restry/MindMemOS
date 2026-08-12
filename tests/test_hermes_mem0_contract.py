from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "mindmemos" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("mindmemos_mem0_contract", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_accepts_mem0_token_environment_variable():
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "http://memory.lan/mcp"},
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )

    assert provider.is_available() is True


def test_single_authorized_topic_is_selected_and_used_for_recall(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "http://memory.lan/mcp",
            "auto_ingest": False,
            "recall_limit": 3,
        },
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )
    calls: list[tuple[str, dict]] = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        if name == "list_topics":
            return json.dumps(
                {"topics": [{"id": "topic-family", "name": "家庭"}], "count": 1},
                ensure_ascii=False,
            )
        if name == "whoami":
            return "用户画像"
        if name == "recall":
            return json.dumps(
                {
                    "results": [
                        {
                            "id": "memory-1",
                            "memory": "Hermes 使用本地 mem0-memory-service。",
                            "topic_id": "topic-family",
                            "updated_at": "2026-08-12T00:00:00Z",
                        }
                    ],
                    "count": 1,
                },
                ensure_ascii=False,
            )
        raise AssertionError(name)

    provider._call_mcp = fake_call
    provider.initialize(
        "session-1",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )

    capsule = provider.prefetch("Hermes 现在使用什么记忆服务？", session_id="session-1")

    assert "本地 mem0-memory-service" in capsule
    assert calls == [
        ("list_topics", {}),
        ("whoami", {}),
        (
            "recall",
            {
                "query": "Hermes 现在使用什么记忆服务？",
                "limit": 3,
                "topic": "topic-family",
            },
        ),
    ]


def test_multiple_topics_without_explicit_choice_do_not_guess(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "http://memory.lan/mcp", "auto_ingest": False},
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )
    calls: list[tuple[str, dict]] = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        if name == "list_topics":
            return json.dumps(
                {
                    "topics": [
                        {"id": "topic-a", "name": "A"},
                        {"id": "topic-b", "name": "B"},
                    ],
                    "count": 2,
                }
            )
        if name == "whoami":
            return "用户画像"
        raise AssertionError(name)

    provider._call_mcp = fake_call
    provider.initialize(
        "session-2",
        hermes_home=str(tmp_path),
        agent_context="primary",
    )

    assert provider.prefetch("查询一个需要记忆的问题", session_id="session-2") == ""
    assert calls == [("list_topics", {}), ("whoami", {})]


def test_explicit_memory_write_uses_mem0_contract(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "http://memory.lan/mcp",
            "topic": "topic-family",
            "background_flush": False,
        },
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )
    calls: list[tuple[str, dict]] = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        if name == "list_topics":
            return json.dumps(
                {"topics": [{"id": "topic-family", "name": "家庭"}], "count": 1}
            )
        if name == "whoami":
            return "用户画像"
        if name == "remember":
            return json.dumps({"ok": True})
        raise AssertionError(name)

    provider._call_mcp = fake_call
    provider.initialize(
        "session-3",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
    )
    provider.on_memory_write(
        "add",
        "user",
        "用户偏好简短、直接的回复。",
        metadata={"session_id": "session-3"},
    )
    provider._flush_spool_once()

    assert calls[-1] == (
        "remember",
        {
            "memory": "用户偏好简短、直接的回复。",
            "topic": "topic-family",
            "source": "hermes-agent",
            "source_thread_id": "session-3",
        },
    )


def test_mcp_structured_content_is_preferred_over_empty_text_content():
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "http://memory.lan/mcp"},
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )
    provider._post_json = lambda url, payload: {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [],
            "structuredContent": {"results": [], "count": 0},
            "isError": False,
        },
    }

    returned = provider._call_mcp("recall", {"query": "x", "topic": "topic-family"})

    assert json.loads(returned) == {"results": [], "count": 0}


def test_mcp_requests_send_the_documented_protocol_version(monkeypatch: pytest.MonkeyPatch):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "http://memory.lan/mcp"},
        environ={"MEM0_MCP_TOKEN": "test-token"},
    )
    captured = {}

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    provider._post_json("http://memory.lan/mcp", {"jsonrpc": "2.0", "id": 1})

    assert captured["request"].get_header("Mcp-protocol-version") == "2025-11-25"


def test_setup_schema_does_not_offer_removed_turn_ingest_endpoint():
    module = load_plugin()
    provider = module.MindMemOSProvider()

    schema = {field["key"]: field for field in provider.get_config_schema()}

    assert "ingest_url" not in schema
    assert "min_write_chars" not in schema
    assert schema["auto_ingest"]["default"] == "false"
