from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "adapters" / "hermes" / "mindmemos" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("mindmemos_plugin_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_provider_is_available_with_instance_key():
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "https://memory.example/mcp"},
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    assert provider.name == "mindmemos"
    assert provider.is_available() is True
    assert provider.get_tool_schemas() == []


def test_provider_rejects_removed_direct_api_configuration():
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"base_url": "http://127.0.0.1:8000"},
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )

    assert provider.is_available() is False
    assert not hasattr(provider, "_legacy_mode")


def test_initialize_injects_whoami_and_prefetch_recalls(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "recall_limit": 3,
            "auto_ingest": False,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    calls = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        if name == "whoami":
            return "用户偏好只用中文回复。"
        if name == "recall":
            return json.dumps(
                {
                    "query": arguments["query"],
                    "memories": [
                        {
                            "id": "hallmark-erpnext",
                            "memory": "Hallmark 使用 ERPNext 作为底座。",
                            "memory_type": "fact",
                            "last_update_at": "2026-08-08 10:00:00",
                        }
                    ],
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

    assert "用户偏好只用中文回复" in provider.system_prompt_block()
    recalled = provider.prefetch("Hallmark 项目的底座是什么？", session_id="session-1")
    assert "Hallmark 使用 ERPNext 作为底座" in recalled
    assert calls == [
        ("whoami", {}),
        (
            "recall",
            {
                "query": "Hallmark 项目的底座是什么？",
                "limit": 3,
                "response_format": "json",
            },
        ),
    ]
    audit_path = tmp_path / "mindmemos-recall-audit.jsonl"
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["provider"]["candidate_ids"] == ["hallmark-erpnext"]
    assert audit["provider"]["injected_ids"] == ["hallmark-erpnext"]
    assert audit["injected_chars"] == len(recalled)
    assert audit["elapsed_ms"] >= 0
    assert audit_path.stat().st_mode & 0o777 == 0o600


def test_prefetch_skips_slash_commands_without_calling_mcp(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "https://memory.example/mcp", "auto_ingest": False},
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    calls = []

    def fake_call(name, arguments):
        calls.append((name, arguments))
        return "profile"

    provider._call_mcp = fake_call
    provider.initialize("slash-session", hermes_home=str(tmp_path), agent_context="primary")
    assert provider.prefetch("/help", session_id="slash-session") == ""
    assert calls == [("whoami", {})]


def test_auto_capsule_is_short_deduplicated_and_persistent(tmp_path):
    module = load_plugin()
    records = [
        {
            "id": f"memory-{index}",
            "memory": (
                "背景说明与当前问题无关。" * 30
                + f"Hermes Prompt Cache 通过 api_content 稳定重放，这是第 {index} 条证据。"
            ),
            "memory_type": "fact",
            "last_update_at": "2026-08-08 10:00:00",
        }
        for index in range(4)
    ]
    recall_calls = []

    def fake_call(name, arguments):
        if name == "whoami":
            return "profile"
        recall_calls.append(arguments)
        return json.dumps({"query": arguments["query"], "memories": records}, ensure_ascii=False)

    config = {
        "mcp_url": "https://memory.example/mcp",
        "auto_ingest": False,
        "auto_context_max_items": 3,
        "auto_context_chars": 1800,
        "auto_memory_chars": 560,
        "session_context_chars": 6000,
    }
    provider = module.MindMemOSProvider(
        config=config,
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = fake_call
    provider.initialize(
        "capsule-session",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
    )

    first = provider.prefetch("Hermes Prompt Cache 如何保持稳定？", session_id="capsule-session")
    assert first.startswith("## MindMemOS 记忆胶囊")
    assert first.count("\n- [fact]") == 3
    assert len(first) <= 1800
    assert "api_content 稳定重放" in first
    assert len(recall_calls) == 1

    # Exact repeated queries skip even the MCP call instead of leaking lower-ranked tail records.
    assert provider.prefetch("Hermes Prompt Cache 如何保持稳定？", session_id="capsule-session") == ""
    assert len(recall_calls) == 1

    state_files = list((tmp_path / "mindmemos-capsules").glob("*.json"))
    assert len(state_files) == 1
    assert state_files[0].stat().st_mode & 0o777 == 0o600

    # A new provider process reloads the capsule state and preserves deduplication.
    restarted_calls = []
    restarted = module.MindMemOSProvider(
        config=config,
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )

    def restarted_call(name, arguments):
        if name == "whoami":
            return "profile"
        restarted_calls.append(arguments)
        return json.dumps({"query": arguments["query"], "memories": records}, ensure_ascii=False)

    restarted._call_mcp = restarted_call
    restarted.initialize(
        "capsule-session",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
    )
    assert restarted.prefetch("Hermes Prompt Cache 如何保持稳定？", session_id="capsule-session") == ""
    assert restarted_calls == []


def test_updated_memory_can_reenter_capsule_for_a_new_query(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "auto_ingest": False,
            "query_cache_seconds": 0,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    version = {"value": "v1"}

    def fake_call(name, arguments):
        if name == "whoami":
            return "profile"
        return json.dumps(
            {
                "query": arguments["query"],
                "memories": [
                    {
                        "id": "same-memory",
                        "memory": f"Hermes 压缩模型版本是 {version['value']}。",
                        "memory_type": "fact",
                        "last_update_at": version["value"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    provider._call_mcp = fake_call
    provider.initialize(
        "updated-session",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
    )
    query = "Hermes 压缩模型是什么？"
    assert "v1" in provider.prefetch(query, session_id="updated-session")
    version["value"] = "v2"
    assert "v2" in provider.prefetch(query, session_id="updated-session")


def test_default_constructor_loads_profile_config(tmp_path):
    module = load_plugin()
    config = {
        "mcp_url": "https://memory.example/mcp",
        "ingest_url": "https://memory.example/ingest/turn",
        "recall_limit": 5,
    }
    (tmp_path / "mindmemos.json").write_text(json.dumps(config), encoding="utf-8")
    provider = module.MindMemOSProvider(
        environ={
            "HERMES_HOME": str(tmp_path),
            "MINDMEMOS_API_KEY": "test-key",
        }
    )
    assert provider.is_available() is True
    assert provider._mcp_url == "https://memory.example/mcp"
    assert provider._config["ingest_url"] == "https://memory.example/ingest/turn"


def test_sync_turn_spools_completed_primary_turn_before_network(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "auto_ingest": True,
            "background_flush": False,
            "min_write_chars": 1,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = lambda name, arguments: "profile" if name == "whoami" else ""
    provider.initialize(
        "session-2",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )

    provider.sync_turn(
        "用户只允许保存最终消息。",
        "已确认，只保存最终答复。",
        session_id="session-2",
        messages=[
            {"role": "assistant", "tool_calls": [{"secret": "must-not-spool"}]},
            {"role": "tool", "content": "must-not-spool"},
        ],
    )

    files = list((tmp_path / "mindmemos-spool").glob("*.json"))
    assert len(files) == 1
    queued = json.loads(files[0].read_text())
    assert queued["kind"] == "turn"
    assert queued["payload"]["user_message"] == "用户只允许保存最终消息。"
    assert queued["payload"]["assistant_message"] == "已确认，只保存最终答复。"
    serialized = json.dumps(queued, ensure_ascii=False)
    assert "must-not-spool" not in serialized
    assert "client_id" not in serialized
    assert "agent_id" not in serialized


def test_spool_is_deleted_only_after_collector_ack(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "auto_ingest": True,
            "background_flush": False,
            "min_write_chars": 1,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = lambda name, arguments: "profile" if name == "whoami" else ""
    provider.initialize(
        "session-3",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )
    provider.sync_turn("保留直到确认。", "收到。", session_id="session-3")
    spool = list((tmp_path / "mindmemos-spool").glob("*.json"))
    assert len(spool) == 1

    provider._send_turn = lambda payload: False
    provider._flush_spool_once()
    assert spool[0].exists()

    provider._send_turn = lambda payload: True
    provider._flush_spool_once()
    assert not spool[0].exists()


def test_initialize_replays_spool_left_by_previous_process(tmp_path):
    module = load_plugin()
    spool_dir = tmp_path / "mindmemos-spool"
    spool_dir.mkdir()
    queued = spool_dir / "hermes-retry.json"
    queued.write_text(
        json.dumps(
            {
                "kind": "turn",
                "payload": {
                    "event_id": "hermes-retry",
                    "session_id": "old-session",
                    "turn_id": "old-turn",
                    "user_message": "断网前的消息",
                    "assistant_message": "断网前的答复",
                    "safe_context": {"runtime": "hermes-agent"},
                },
            },
            ensure_ascii=False,
        )
    )
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "background_flush": True,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = lambda name, arguments: "profile" if name == "whoami" else ""
    provider._send_turn = lambda payload: True
    provider.initialize(
        "new-session",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )

    deadline = time.time() + 2
    while queued.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert not queued.exists()


def test_memory_write_is_spooled_and_mindmemos_provenance_is_ignored(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "background_flush": False,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = lambda name, arguments: "profile" if name == "whoami" else ""
    provider.initialize(
        "session-4",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )

    provider.on_memory_write(
        "add",
        "user",
        "用户偏好所有回复只用中文。",
        metadata={"write_origin": "memory_tool", "session_id": "session-4"},
    )
    files = list((tmp_path / "mindmemos-spool").glob("*.json"))
    assert len(files) == 1
    queued = json.loads(files[0].read_text())
    assert queued["kind"] == "remember"
    assert queued["content"] == "用户偏好所有回复只用中文。"

    provider.on_memory_write(
        "add",
        "memory",
        "MindMemOS recall 返回的旧记忆。",
        metadata={"write_origin": "mindmemos"},
    )
    assert len(list((tmp_path / "mindmemos-spool").glob("*.json"))) == 1


def test_explicit_memory_spool_is_delivered_through_remember(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "background_flush": False,
        },
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    provider._call_mcp = lambda name, arguments: "profile" if name == "whoami" else ""
    provider.initialize(
        "session-5",
        hermes_home=str(tmp_path),
        platform="feishu",
        agent_context="primary",
        agent_identity="default",
    )
    provider.on_memory_write(
        "add",
        "user",
        "用户偏好架构图单页不超过三屏。",
        metadata={"session_id": "session-5"},
    )
    spool = list((tmp_path / "mindmemos-spool").glob("*.json"))
    assert len(spool) == 1
    calls = []

    def deliver(name, arguments):
        calls.append((name, arguments))
        return "已保存" if name == "remember" else ""

    provider._call_mcp = deliver
    provider._flush_spool_once()
    assert not spool[0].exists()
    assert calls == [
        (
            "remember",
            {
                "content": "用户偏好架构图单页不超过三屏。",
                "session_id": "session-5",
            },
        )
    ]


def test_setup_schema_keeps_secret_out_of_profile_json(tmp_path):
    module = load_plugin()
    provider = module.MindMemOSProvider(
        config={"mcp_url": "https://memory.example/mcp"},
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    schema = {field["key"]: field for field in provider.get_config_schema()}
    assert schema["api_key"]["secret"] is True
    assert schema["api_key"]["env_var"] == "MINDMEMOS_API_KEY"

    provider.save_config(
        {
            "api_key": "must-not-persist",
            "mcp_url": "https://memory.example/mcp",
            "ingest_url": "https://memory.example/ingest/turn",
            "recall_limit": 6,
            "auto_ingest": True,
        },
        str(tmp_path),
    )
    saved = json.loads((tmp_path / "mindmemos.json").read_text())
    assert saved["mcp_url"] == "https://memory.example/mcp"
    assert "api_key" not in saved
