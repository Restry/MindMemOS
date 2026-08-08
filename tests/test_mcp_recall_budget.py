from __future__ import annotations

import importlib.util
from pathlib import Path

MCP_SERVER = Path(__file__).resolve().parents[1] / "mcp_server.py"


def load_mcp_server():
    spec = importlib.util.spec_from_file_location("mcp_server_budget_under_test", MCP_SERVER)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_recall_forces_rerank_and_caps_complete_records(monkeypatch):
    module = load_mcp_server()
    seen = {}
    memories = [{"memory": f"memory-{index}-" + ("x" * 600), "memory_type": "fact"} for index in range(12)]

    def fake_post(path, body):
        seen.update(body)
        return {"data": {"memories": memories}}

    monkeypatch.setattr(module, "_post", fake_post)
    output = module.t_recall({"query": "project history", "top_k": 99})

    assert seen["rerank"] is True
    assert seen["score_threshold"] == 0.1
    assert seen["top_k"] == 8
    assert output.startswith("查到 6 条相关记忆：")
    assert "memory-5-" in output
    assert "memory-6-" not in output
    assert len(output) <= 4000
    assert not output.endswith("…")
