#!/usr/bin/env python3
"""Import human-reviewed atomic memories without another LLM transformation.

The script uses MindMemOS' official database writer so dense/sparse vectors and
Neo4j memory/entity nodes are written consistently. Original long memories are
soft-archived only after every curated fact is either written and verified or
reused from an exact active duplicate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import yaml
from mindmemos.config import init_config_from_env
from mindmemos.pipelines.add.default import DefaultAddPipeline
from mindmemos.typing import (
    REL_DERIVED_FROM,
    REL_EXTRACTED_FROM,
    AddPipelineInput,
    GraphNodeRef,
    GraphRelationship,
    MemoryDbMutationPlan,
    MemoryRequestContext,
)

ROOT = Path(__file__).resolve().parents[1]
QDRANT = os.getenv("MINDMEMOS_QDRANT", "http://127.0.0.1:6333").rstrip("/")
API = os.getenv("MINDMEMOS_MEMORY_API", "http://127.0.0.1:8000/v1/memory").rstrip("/")
COLLECTION = "memory_item_v1"
MIGRATION = "historical_fallback_curated_over_1000_v2"
API_KEYS = Path(os.getenv("MINDMEMOS_API_KEYS", str(ROOT / "config/mindmemos/api_keys.yaml")))
MAX_FACT_CHARS = 200
INHERITED_SOURCE_KEYS = (
    "source_id",
    "source_type",
    "source_role",
    "source_raw_role",
    "source_message_index",
    "source_timestamp_ms",
    "source",
    "doc",
    "file_name",
    "file_path",
    "chunk_id",
    "section",
)


def atomic_json(path: Path, value: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def initialize_runtime() -> None:
    init_config_from_env()


def post_json(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {text[:1000]}") from exc


def scroll() -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {"limit": 1000, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = post_json(f"{QDRANT}/collections/{COLLECTION}/points/scroll", body)["result"]
        points.extend(result.get("points") or [])
        offset = result.get("next_page_offset")
        if offset is None:
            return points


def load_auth() -> dict[str, Any]:
    cfg = yaml.safe_load(API_KEYS.read_text()) or {}
    for item in cfg.get("api_keys") or []:
        if item.get("enabled") and item.get("memory_algorithm") == "vanilla":
            return item
    raise RuntimeError("No enabled vanilla API key")


def load_items(files: list[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        data = json.loads(path.read_text())
        for item in data.get("items") or []:
            memory_id = str(item.get("original_memory_id") or "")
            if not memory_id or memory_id in seen:
                raise RuntimeError(f"missing or duplicate original_memory_id: {memory_id}")
            seen.add(memory_id)
            decision = item.get("decision")
            facts = [str(value).strip() for value in item.get("atomic_facts") or []]
            if decision not in {"curated", "discard"}:
                raise RuntimeError(f"invalid decision for {memory_id}: {decision}")
            if decision == "curated" and not facts:
                raise RuntimeError(f"curated item has no facts: {memory_id}")
            for fact in facts:
                if not fact or len(fact) > MAX_FACT_CHARS:
                    raise RuntimeError(f"invalid fact length for {memory_id}: {len(fact)}")
            item["atomic_facts"] = facts
            items.append(item)
    return items


def timestamp_ms(payload: dict[str, Any]) -> int | None:
    value = payload.get("created_at")
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except Exception:
        return None


def archive_original(api_key: str, memory_id: str) -> None:
    response = post_json(
        f"{API}/delete",
        {"memory_id": memory_id},
        {"Authorization": f"Bearer {api_key}"},
    )
    if response.get("code") != "ok":
        raise RuntimeError(f"archive failed for {memory_id}: {response.get('message')}")


def active_content_map(points: list[dict[str, Any]], project_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for point in points:
        payload = point.get("payload") or {}
        if payload.get("project_id") != project_id or str(payload.get("status") or "active") != "active":
            continue
        content = str(payload.get("content") or "").strip()
        memory_id = str(payload.get("memory_id") or point.get("id"))
        if content:
            result.setdefault(content, memory_id)
    return result


def retrieve(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    result = post_json(
        f"{QDRANT}/collections/{COLLECTION}/points",
        {"ids": ids, "with_payload": True, "with_vector": True},
    )
    return result.get("result") or []


def context_for(payload: dict[str, Any], auth: dict[str, Any], original_id: str) -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id=f"curated-reextract:{original_id}",
        account_id=str(payload.get("account_id") or "memory_standalone"),
        project_id=str(auth["project_id"]),
        api_key_uuid=str(auth["key_id"]),
        memory_algorithm=str(auth.get("memory_algorithm") or "vanilla"),
        scopes=list(auth.get("scopes") or []),
        user_id=payload.get("user_id"),
        app_id=payload.get("app_id"),
        session_id=payload.get("session_id"),
        agent_id=payload.get("agent_id"),
    )


def attach_inherited_lineage(
    plan: Any,
    original_id: str,
    payload: dict[str, Any],
    context: MemoryRequestContext,
) -> None:
    """Inherit the original source and record evidence plus transform lineage."""

    original_metadata = dict(payload.get("metadata") or {})
    source_id = str(original_metadata.get("source_id") or "")
    if not source_id:
        raise RuntimeError(f"missing source_id for original memory {original_id}")
    inherited = {
        key: original_metadata[key]
        for key in INHERITED_SOURCE_KEYS
        if key in original_metadata and original_metadata[key] is not None
    }
    for memory in plan.memories:
        memory.metadata = {**dict(memory.metadata or {}), **inherited, "source_inherited": True}
        plan.relationships.extend(
            [
                GraphRelationship(
                    source=GraphNodeRef(kind="Memory", project_id=context.project_id, node_id=memory.memory_id),
                    target=GraphNodeRef(kind="Source", project_id=context.project_id, node_id=source_id),
                    rel_type=REL_EXTRACTED_FROM,
                    project_id=context.project_id,
                    extraction_position={"message_index": original_metadata.get("source_message_index")},
                    metadata={
                        "source_type": original_metadata.get("source_type"),
                        "role": original_metadata.get("source_role"),
                        "inherited_from_memory_id": original_id,
                        "migration": MIGRATION,
                    },
                ),
                GraphRelationship(
                    source=GraphNodeRef(kind="Memory", project_id=context.project_id, node_id=memory.memory_id),
                    target=GraphNodeRef(kind="Memory", project_id=context.project_id, node_id=original_id),
                    rel_type=REL_DERIVED_FROM,
                    project_id=context.project_id,
                    metadata={"migration": MIGRATION, "curated": True},
                ),
            ]
        )


async def write_curated(
    pipeline: DefaultAddPipeline,
    original_id: str,
    payload: dict[str, Any],
    facts: list[str],
    auth: dict[str, Any],
    exact: dict[str, str],
) -> tuple[list[str], list[str]]:
    normalized: list[str] = []
    reused: list[str] = []
    new_facts: list[str] = []
    for fact in facts:
        value = pipeline._text_preprocessor.preprocess_text(fact, include_entities=False).normalized_text
        if not value:
            raise RuntimeError(f"fact normalized empty for {original_id}")
        normalized.append(value)
        existing = exact.get(value)
        if existing:
            reused.append(existing)
        else:
            new_facts.append(value)
    if not new_facts:
        return [], reused

    inp = AddPipelineInput(
        messages=[{"role": "user", "content": fact} for fact in new_facts],
        mode="sync",
        timestamp=timestamp_ms(payload),
        metadata={"migration": MIGRATION, "original_memory_id": original_id, "curated": True},
    )
    ctx = context_for(payload, auth, original_id)
    plan, _ = pipeline._build_plan(inp, ctx)
    if len(plan.memories) != len(new_facts):
        raise RuntimeError(f"plan count mismatch for {original_id}")

    id_map: dict[str, str] = {}
    created_ids: list[str] = []
    for memory, fact in zip(plan.memories, new_facts, strict=True):
        old_id = memory.memory_id
        fact_hash = hashlib.sha256(fact.encode()).hexdigest()
        new_id = str(uuid5(NAMESPACE_URL, f"{MIGRATION}:{original_id}:{fact_hash}"))
        id_map[old_id] = new_id
        created_ids.append(new_id)
        memory.memory_id = new_id
        memory.parent_ids = [original_id]
        roots = payload.get("root_id") or [original_id]
        memory.root_id = [str(value) for value in roots]
        memory.mem_extract_type = "curated_reextract"
        memory.mem_extract_version = "curated_over_1000_v2"
        memory.metadata = {
            **dict(memory.metadata or {}),
            "migration": MIGRATION,
            "original_memory_id": original_id,
            "curated": True,
            "curated_fact_hash": fact_hash,
        }
    for vector in plan.vectors:
        vector.memory_id = id_map.get(vector.memory_id, vector.memory_id)
    for relationship in plan.relationships:
        if relationship.source.kind == "Memory" and relationship.source.node_id in id_map:
            relationship.source.node_id = id_map[relationship.source.node_id]
        if relationship.target.kind == "Memory" and relationship.target.node_id in id_map:
            relationship.target.node_id = id_map[relationship.target.node_id]

    attach_inherited_lineage(plan, original_id, payload, ctx)

    embed_client = pipeline.db_writer._ensure_embed_client()
    for memory, vector in zip(plan.memories, plan.vectors, strict=True):
        response = await embed_client.embed(task="memory.curated_reextract", text=memory.content)
        if not response.embeddings or not response.embeddings[0]:
            raise RuntimeError(f"embedding empty for {memory.memory_id}")
        vector.semantic_vector = response.embeddings[0]

    result = await pipeline.db_writer.apply_mutation_plan(
        ctx,
        MemoryDbMutationPlan.from_write_plan(plan),
        consistency="strong",
    )
    if result.errors or result.graph_pending:
        raise RuntimeError(f"database write incomplete for {original_id}: {result.errors}")

    records = retrieve(created_ids)
    by_id = {str((point.get("payload") or {}).get("memory_id") or point.get("id")): point for point in records}
    if set(by_id) != set(created_ids):
        raise RuntimeError(f"verification missing IDs for {original_id}")
    expected = {memory.memory_id: memory.content for memory in plan.memories}
    for memory_id, content in expected.items():
        point = by_id[memory_id]
        point_payload = point.get("payload") or {}
        vectors = point.get("vector") or {}
        if point_payload.get("status") != "active" or point_payload.get("content") != content:
            raise RuntimeError(f"verification payload mismatch for {memory_id}")
        if point_payload.get("mem_extract_type") != "curated_reextract":
            raise RuntimeError(f"verification extractor mismatch for {memory_id}")
        if not vectors:
            raise RuntimeError(f"verification vectors missing for {memory_id}")
        exact[content] = memory_id
    return created_ids, reused


async def run(args: argparse.Namespace) -> None:
    initialize_runtime()
    manifest = json.loads(args.manifest.read_text())
    state_path = args.manifest.parent / "state.json"
    state = json.loads(state_path.read_text())
    auth = load_auth()
    if manifest.get("project_id") != auth.get("project_id"):
        raise RuntimeError("manifest project does not match configured API key")
    points = manifest.get("points") or []
    originals = {
        str((point.get("payload") or {}).get("memory_id") or point.get("id")): point.get("payload") or {}
        for point in points
    }
    items = load_items(args.curated)
    all_points = scroll()
    exact = active_content_map(all_points, str(auth["project_id"]))
    pipeline = DefaultAddPipeline()

    for index, item in enumerate(items, start=1):
        original_id = item["original_memory_id"]
        if original_id not in originals or original_id not in state["items"]:
            raise RuntimeError(f"unknown original memory: {original_id}")
        state_item = state["items"][original_id]
        if item["decision"] == "discard":
            archive_original(str(auth["api_key"]), original_id)
            state_item.update(
                {
                    "phase": "discarded",
                    "review": "discard",
                    "curated_decision_at": datetime.now().isoformat(),
                    "error": None,
                }
            )
            atomic_json(state_path, state)
            print(json.dumps({"progress": f"{index}/{len(items)}", "memory_id": original_id, "decision": "discard"}))
            continue

        try:
            created, reused = await write_curated(
                pipeline,
                original_id,
                originals[original_id],
                item["atomic_facts"],
                auth,
                exact,
            )
            archive_original(str(auth["api_key"]), original_id)
            state_item.update(
                {
                    "phase": "curated_archived",
                    "review": "curated_approved",
                    "curated_memory_ids": created,
                    "reused_memory_ids": reused,
                    "curated_fact_count": len(item["atomic_facts"]),
                    "curated_at": datetime.now().isoformat(),
                    "error": None,
                }
            )
            atomic_json(state_path, state)
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(items)}",
                        "memory_id": original_id,
                        "decision": "curated",
                        "created": len(created),
                        "reused": len(reused),
                    }
                ),
                flush=True,
            )
        except Exception as exc:
            state_item.update({"phase": "curated_error", "error": str(exc)[:2000]})
            atomic_json(state_path, state)
            print(json.dumps({"progress": f"{index}/{len(items)}", "memory_id": original_id, "error": str(exc)[:500]}))
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--curated", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
