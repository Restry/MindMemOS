from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "adapters/hermes/mindmemos/__init__.py"


def _load_plugin(monkeypatch: pytest.MonkeyPatch):
    agent_package = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.MemoryProvider = object
    tools_package = types.ModuleType("tools")
    registry = types.ModuleType("tools.registry")
    registry.tool_error = lambda message: message
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)

    spec = importlib.util.spec_from_file_location("test_hermes_mindmemos_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_hermes_primary_turns_and_builtin_writes_use_durable_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _load_plugin(monkeypatch)
    spool = tmp_path / "hermes-spool.sqlite3"
    config = {
        "base_url": "http://127.0.0.1:8000",
        "api_key": "api-key-not-logged",
        "user_id": "leway",
        "write_enabled": True,
        "min_write_chars": 1,
        "ingest_url": "http://127.0.0.1:8765",
        "ingest_key": "",
        "ingest_spool": str(spool),
        "ingest_client_module": str(ROOT / "adapters/python/mindmemos_ingest_client.py"),
    }
    (tmp_path / "mindmemos.json").write_text(json.dumps(config))

    provider = plugin.MindMemOSProvider()
    provider.initialize(
        "session-1",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="primary",
    )
    messages = [
        {"role": "user", "content": "durable user fact"},
        {"role": "assistant", "content": "durable assistant answer"},
    ]
    provider.sync_turn(
        "durable user fact",
        "durable assistant answer",
        session_id="session-1",
        messages=messages,
    )
    provider.sync_turn(
        "durable user fact",
        "durable assistant answer",
        session_id="session-1",
        messages=messages,
    )
    provider.sync_turn(
        "must not recurse",
        "ignored",
        session_id="session-1",
        messages=[{"metadata": {"provenance": {"capture_mode": "auto_hook"}}}],
    )
    provider.on_memory_write("add", "memory", "explicit builtin memory", {})
    provider.shutdown()

    with sqlite3.connect(spool) as connection:
        rows = connection.execute("SELECT endpoint, payload_json, status FROM events ORDER BY endpoint").fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"/ingest/memory", "/ingest/turn"}
    turn = next(json.loads(row[1]) for row in rows if row[0] == "/ingest/turn")
    assert turn["user_message"] == "durable user fact"
    assert turn["assistant_message"] == "durable assistant answer"
    assert turn["safe_context"] == {"runtime": "hermes", "platform": "cli"}
    assert all(row[2] == "pending" for row in rows)


def test_prefetch_skips_only_low_information_chat_acknowledgements(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _load_plugin(monkeypatch)
    provider = plugin.MindMemOSProvider()
    provider._enabled = True
    provider._cfg = {"top_k": 6, "score_threshold": 0.1}
    searched: list[str] = []
    provider._search = lambda query, **_kwargs: searched.append(query) or []

    for query in ("好的", "谢谢", "继续", "可以", "嗯，可以", "收到。"):
        assert provider.prefetch(query) == ""
    assert searched == []

    provider.prefetch("继续检查 MindMemOS 性能")
    assert searched == ["继续检查 MindMemOS 性能"]


def test_auto_capture_uses_the_same_exact_low_information_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    provider = plugin.MindMemOSProvider()
    provider._cfg = {"min_write_chars": 24}

    assert provider._worth_writing("对 MindMemOS 当前性能做一次完整检查并记录所有真实结果")
    assert provider._worth_writing("嗯这个问题需要继续调查服务端队列以及失败自动重放路径")
    assert not provider._worth_writing("/remember 这是一条足够长但属于命令的输入，不应自动捕获")
    assert not provider._worth_writing("好的")


def test_repo_owned_hermes_provider_installs_exactly(tmp_path: Path) -> None:
    installer_path = ROOT / "adapters/hermes/install.py"
    spec = importlib.util.spec_from_file_location("test_hermes_provider_installer", installer_path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    result = installer.install(tmp_path)
    assert result["ok"] is True
    for name in ("__init__.py", "plugin.yaml"):
        assert (tmp_path / "plugins/mindmemos" / name).read_bytes() == (
            ROOT / "adapters/hermes/mindmemos" / name
        ).read_bytes()
    assert not (tmp_path / "mindmemos.json").exists()


def test_llms_txt_documents_hermes_provider_lifecycle_and_source() -> None:
    document = (ROOT / "llms.txt").read_text(encoding="utf-8")
    server = (ROOT / "mcp_http_server.py").read_text(encoding="utf-8")

    for required in (
        "Hermes native Memory Provider",
        "adapters/hermes/mindmemos/__init__.py",
        "python3 adapters/hermes/install.py --check",
        "hermes config set memory.provider mindmemos",
        "hermes config set memory.provider builtin",
        "$HERMES_HOME/mindmemos.json",
        "只配置 MCP 或安装 Skill，**不等于 Hermes Memory Provider 已接管**",
    ):
        assert required in document
    assert 'LLMS_FILE = os.path.join(HERE, "llms.txt")' in server
    assert "with open(LLMS_FILE" in server
    assert 'return f"""# MindMemOS MCP' not in server
