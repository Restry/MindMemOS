from __future__ import annotations

import importlib.util
import json
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


def test_structured_recall_preserves_ids_and_does_not_change_text_default(monkeypatch):
    module = load_mcp_server()
    memories = [
        {
            "id": "memory-123",
            "memory": "Hermes 使用稳定的 api_content 保持 Prompt Cache。",
            "memory_type": "fact",
            "last_update_at": "2026-08-08 10:00:00",
        }
    ]
    monkeypatch.setattr(module, "_post", lambda path, body: {"data": {"memories": memories}})

    structured = json.loads(module.t_recall({"query": "Hermes Prompt Cache", "top_k": 8, "response_format": "json"}))
    assert structured["memories"][0]["id"] == "memory-123"
    assert structured["memories"][0]["memory"] == memories[0]["memory"]
    assert structured["memories"][0]["last_update_at"] == "2026-08-08 10:00:00"

    text = module.t_recall({"query": "Hermes Prompt Cache", "top_k": 8})
    assert text.startswith("查到 1 条相关记忆：")
    assert "memory-123" not in text
