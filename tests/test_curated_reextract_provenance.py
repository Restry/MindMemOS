from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/mindmemos_import_curated_reextract.py"


def _load_importer():
    spec = importlib.util.spec_from_file_location("test_curated_reextract_importer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_curated_reextract_inherits_source_and_adds_lineage_edges() -> None:
    importer = _load_importer()
    memory = SimpleNamespace(memory_id="new-memory", metadata={"content_hash": "fact-hash"})
    plan = SimpleNamespace(memories=[memory], relationships=[])
    payload = {
        "metadata": {
            "source_id": "original-source",
            "source_type": "message",
            "source_role": "user",
            "source_message_index": 0,
            "source": "hermes_turn",
            "doc": "项目主档",
        }
    }
    context = SimpleNamespace(project_id="project")

    importer.attach_inherited_lineage(plan, "original-memory", payload, context)

    assert memory.metadata["source_id"] == "original-source"
    assert memory.metadata["source_type"] == "message"
    assert memory.metadata["source"] == "hermes_turn"
    assert memory.metadata["doc"] == "项目主档"
    assert memory.metadata["content_hash"] == "fact-hash"
    edges = {(edge.rel_type, edge.source.node_id, edge.target.kind, edge.target.node_id) for edge in plan.relationships}
    assert edges == {
        ("EXTRACTED_FROM", "new-memory", "Source", "original-source"),
        ("DERIVED_FROM", "new-memory", "Memory", "original-memory"),
    }


def test_curated_reextract_refuses_to_write_without_original_source() -> None:
    importer = _load_importer()
    plan = SimpleNamespace(memories=[SimpleNamespace(memory_id="new-memory", metadata={})], relationships=[])

    with pytest.raises(RuntimeError, match="missing source_id"):
        importer.attach_inherited_lineage(
            plan,
            "original-memory",
            {"metadata": {}},
            SimpleNamespace(project_id="project"),
        )


def test_curated_reextract_initializes_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    importer = _load_importer()
    calls = []
    monkeypatch.setattr(importer, "init_config_from_env", lambda: calls.append("initialized"))

    importer.initialize_runtime()

    assert calls == ["initialized"]


def test_curated_reextract_builds_context_from_original_and_canonical_key_fields() -> None:
    importer = _load_importer()

    context = importer.context_for(
        {"account_id": "memory_standalone", "user_id": "leway", "session_id": "session"},
        {
            "project_id": "project",
            "key_id": "key-id",
            "memory_algorithm": "vanilla",
            "scopes": ["memory:read", "memory:write"],
        },
        "original",
    )

    assert context.account_id == "memory_standalone"
    assert context.project_id == "project"
    assert context.api_key_uuid == "key-id"
