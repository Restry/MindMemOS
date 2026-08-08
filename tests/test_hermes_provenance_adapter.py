from __future__ import annotations

import importlib.util
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


def test_prefetch_skips_only_low_information_chat_acknowledgements(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _load_plugin(monkeypatch)
    provider = plugin.MindMemOSProvider(
        config={"mcp_url": "https://memory.example/mcp"},
        environ={"MINDMEMOS_API_KEY": "test-key"},
    )
    recalled: list[tuple[str, dict]] = []
    provider._call_mcp = lambda name, args: (
        recalled.append((name, args))
        or ('{"query":"q","memories":[{"id":"m1","memory":"memory","memory_type":"fact","last_update_at":"v1"}]}')
    )

    for query in ("好的", "谢谢", "继续", "可以", "嗯，可以", "收到。"):
        assert provider.prefetch(query) == ""
    assert recalled == []

    provider.prefetch("继续检查 MindMemOS 性能")
    assert recalled == [
        (
            "recall",
            {
                "query": "继续检查 MindMemOS 性能",
                "limit": 8,
                "response_format": "json",
            },
        )
    ]


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
        "https://github.com/Restry/MindMemOS",
        "https://github.com/Restry/MindMemOS/tree/main/adapters/hermes/mindmemos",
        "git clone https://github.com/Restry/MindMemOS.git",
        "adapters/hermes/mindmemos/__init__.py",
        "python3 adapters/hermes/install.py --check",
        "hermes config set memory.provider mindmemos",
        "hermes config set memory.provider builtin",
        "$HERMES_HOME/mindmemos.json",
        "只配置 MCP 或安装 Skill，**不等于 Hermes Memory Provider 已接管**",
        "Automatic activation boundary",
        "`llms.txt` 是被动的发现与接入说明",
        "仅把本文件 URL 放进聊天不构成接入",
    ):
        assert required in document
    assert 'LLMS_FILE = os.path.join(HERE, "llms.txt")' in server
    assert "with open(LLMS_FILE" in server
    assert 'return f"""# MindMemOS MCP' not in server
