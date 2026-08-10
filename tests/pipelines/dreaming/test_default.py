from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from mindmemos.config import DreamingConfig, TextProcessingConfig
from mindmemos.infra.db import QdrantRecord
from mindmemos.pipelines.dreaming.default import DefaultDreamingPipeline
from mindmemos.typing.activity import ActivityScope, RecentActivityBundle, WrittenMemoryRef
from mindmemos.typing.algo import ConsolidationAction, ConsolidationCreate, ConsolidationLink, ConsolidationMerge
from mindmemos.typing.memory import GraphNeighborScope, MemoryRequestContext, MemoryView
from mindmemos.typing.service import DreamingPipelineInput


class FakeReader:
    def __init__(self, memories: list[MemoryView]) -> None:
        self.memories = memories
        self.add_record_payloads: dict[str, dict] = {}
        self.neo4j_read_calls: list[tuple[str, dict]] = []
        self.neo4j_rows: list[dict] | None = None
        self._clients = SimpleNamespace(neo4j=SimpleNamespace(run_read=self._run_neo4j_read))

    async def _run_neo4j_read(self, query: str, **params):
        self.neo4j_read_calls.append((query, params))
        if self.neo4j_rows is not None:
            return self.neo4j_rows
        return [
            {
                "entity_id": "entity-1",
                "entity_name": "Alice",
                "entity_type": "person",
                "memory_ids": [memory.memory_id for memory in self.memories],
            }
        ]

    async def get_memories(self, _ctx, memory_ids: list[str]) -> list[MemoryView]:
        requested = set(memory_ids)
        return [memory for memory in self.memories if memory.memory_id in requested]

    async def list_memories(self, _ctx, *, filters=None, limit=50, cursor=None):
        return self.memories[:limit], None

    async def list_add_records(self, _ctx, *, filters=None, limit=50, cursor=None):
        return [], None

    async def get_add_records_by_ids(self, _ctx, add_record_ids: list[str]):
        return [
            QdrantRecord(point_id=add_record_id, payload=self.add_record_payloads.get(add_record_id, {}))
            for add_record_id in add_record_ids
        ]

    async def list_memory_neighbor_scopes(
        self,
        _ctx,
        memory_ids: list[str],
        *,
        edge_filter=None,
        limit_per_entity: int = 50,
        limit_direct_per_memory: int = 20,
        attach_direct_neighbors_to_entity_scopes: bool = True,
    ) -> list[GraphNeighborScope]:
        all_ids = tuple(memory.memory_id for memory in self.memories[:limit_per_entity])
        return [
            GraphNeighborScope(
                seed_memory_id=memory_id,
                entity_id="entity-1",
                entity_name="Alice",
                entity_type="person",
                memory_ids=all_ids,
                source="shared_entity",
            )
            for memory_id in memory_ids
        ]

    async def search_sparse(self, _ctx, _req, *, indices, values):
        return SimpleNamespace(hits=[])


class FakeWriter:
    def __init__(self) -> None:
        self.plans: list[Any] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[object] = []
        self.add_record_patches: list[tuple[str, dict]] = []

    async def apply_mutation_plan(self, _ctx, plan, *, consistency="fast"):
        write_plan = plan.to_write_plan()
        if write_plan.memories or write_plan.relationships:
            self.plans.append(write_plan)
        for command in plan.memory_updates:
            self.updated.append(command)
        for command in plan.memory_deletes:
            self.deleted.append((command.memory_id, command.reason))
        return SimpleNamespace(
            memory_ids=[m.memory_id for m in write_plan.memories],
            mutations=[SimpleNamespace(memory_id=command.memory_id, changed=True) for command in plan.memory_updates]
            + [SimpleNamespace(memory_id=command.memory_id, changed=True) for command in plan.memory_deletes],
            errors=[],
            graph_pending=False,
        )

    async def write(self, _ctx, plan, *, consistency="fast"):
        self.plans.append(plan)
        return SimpleNamespace(memory_ids=[m.memory_id for m in plan.memories])

    async def patch_add_record(self, _ctx, add_record_id, payload):
        self.add_record_patches.append((add_record_id, payload))
        return SimpleNamespace(changed=True)

    async def delete_memory(self, _ctx, req):
        self.deleted.append((req.memory_id, req.reason))
        return SimpleNamespace(changed=True)

    async def update_memory(self, _ctx, req):
        self.updated.append(req)
        return SimpleNamespace(changed=True)


class FakeEmbed:
    async def embed(self, *, task: str, text):
        texts = text if isinstance(text, list) else [text]
        return SimpleNamespace(embeddings=[[0.1, 0.2, 0.3] for _ in texts])


class FakeLLM:
    def __init__(self, action: ConsolidationAction) -> None:
        self.action = action
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        if self.calls % 2 == 1:
            return SimpleNamespace(
                parsed=SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            candidate_type="needs_consolidation",
                            primary_memory_id="m1",
                            neighbor_memory_id="m2",
                            primary_value_hint="",
                            neighbor_value_hint="",
                            confidence="high",
                            reason="test relation",
                        )
                    ]
                )
            )
        return SimpleNamespace(parsed=self.action)


class FakeActivityCollector:
    def __init__(self, memories: list[MemoryView]) -> None:
        self.memories = memories

    async def collect(self, scope: ActivityScope, **_kwargs) -> RecentActivityBundle:
        now = datetime.now(UTC)
        return RecentActivityBundle(
            window_start=now - timedelta(days=1),
            window_end=now,
            scope=scope,
            written_memories=[
                WrittenMemoryRef(
                    memory_id=memory.memory_id,
                    content=memory.content,
                    add_record_ids=[f"add-{memory.memory_id}"],
                    session_id="sess",
                    user_id="user",
                )
                for memory in self.memories
            ],
        )


def ctx() -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="00000000-0000-0000-0000-000000000001",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        session_id="sess",
        scopes=["memory:write"],
    )


def memory(
    memory_id: str,
    *,
    content: str,
    entity_id: str = "entity-1",
    property_name: str = "preference",
    content_hash: str | None = None,
    created_offset: int = 0,
    source_id: str | None = None,
) -> MemoryView:
    now = datetime.now(UTC)
    metadata = {}
    if content_hash:
        metadata["content_hash"] = content_hash
    if source_id:
        metadata["source_id"] = source_id
        metadata["provenance"] = {"capture_mode": "auto_hook", "event_id": f"event-{source_id}"}
    return MemoryView(
        memory_id=memory_id,
        project_id="proj",
        content=content,
        mem_type="fact",
        status="active",
        metadata=metadata,
        root_id=["root-1"],
        entity_id=entity_id,
        entity_type="person",
        property_name=property_name,
        created_at=now - timedelta(minutes=created_offset),
        update_at=now - timedelta(minutes=created_offset),
    )


def pipeline(
    *, memories: list[MemoryView], action: ConsolidationAction
) -> tuple[DefaultDreamingPipeline, FakeReader, FakeWriter]:
    reader = FakeReader(memories)
    writer = FakeWriter()
    pipe = DefaultDreamingPipeline(
        dreaming_config=DreamingConfig(
            lookback_days=7,
            max_scopes_per_run=5,
            max_seed_memories=20,
            max_memories_per_scope=20,
            min_scope_updates=1,
            min_cluster_size=2,
        ),
        text_config=TextProcessingConfig(),
        llm_client=FakeLLM(action),
        embed_client=FakeEmbed(),
        activity_collector=FakeActivityCollector(memories),
        db_reader=reader,
        db_writer=writer,
        consistency="fast",
    )
    return pipe, reader, writer


@pytest.mark.asyncio
async def test_dreaming_archives_exact_duplicates_before_llm_actions():
    memories = [
        memory("m1", content="Alice likes tea", content_hash="same", created_offset=1),
        memory("m2", content="Alice likes tea", content_hash="same", created_offset=2),
    ]
    pipe, reader, writer = pipeline(memories=memories, action=ConsolidationAction())

    result = await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert result.status == "ok"
    assert writer.deleted == [("m2", "duplicate_of:m1")]
    assert writer.plans == []


@pytest.mark.asyncio
async def test_dreaming_skips_done_add_records_when_clustering_hot_memories():
    memories = [
        memory("m1", content="Alice likes tea", created_offset=1),
        memory("m2", content="Alice likes coffee", created_offset=2),
    ]
    pipe, reader, _writer = pipeline(memories=memories, action=ConsolidationAction())
    reader.add_record_payloads = {
        "add-m1": {"consolidation_status": "done"},
        "add-m2": {"consolidation_status": "pending"},
    }

    clusters = await pipe._cluster_hot_memories(ctx())

    assert len(clusters) == 1
    scope, _memories = clusters[0]
    assert scope.primary_memory_id == "m2"
    assert scope.add_record_ids == ("add-m2",)
    query, params = reader.neo4j_read_calls[0]
    assert "neighbor.update_at" not in query
    assert "neighbor.created_at" not in query
    assert "ORDER BY neighbor.memory_id ASC" in query
    assert "LIMIT $entity_probe_limit" in query
    assert params["entity_probe_limit"] == pipe._cfg.max_entity_memory_count + 1


@pytest.mark.asyncio
async def test_dreaming_skips_entity_clusters_over_memory_limit():
    memories = [
        memory("m1", content="Alice likes green tea"),
        memory("m2", content="Alice likes black tea"),
        memory("m3", content="Alice likes jasmine tea"),
    ]
    pipe, reader, _writer = pipeline(memories=memories, action=ConsolidationAction())
    pipe._cfg.max_entity_memory_count = 2

    clusters = await pipe._cluster_hot_memories(ctx())

    assert clusters == []
    _query, params = reader.neo4j_read_calls[0]
    assert params["entity_probe_limit"] == 3


@pytest.mark.asyncio
async def test_dreaming_dedupes_duplicate_clusters_before_llm_calls():
    memories = [
        memory("m1", content="Alice likes green tea", created_offset=2),
        memory("m2", content="Alice prefers jasmine tea", created_offset=1),
    ]
    action = ConsolidationAction()
    pipe, _reader, writer = pipeline(memories=memories, action=action)

    result = await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert result.status == "ok"
    assert pipe._llm_client.calls == 2
    patched_ids = {add_record_id for add_record_id, _payload in writer.add_record_patches}
    assert patched_ids == {"add-m1", "add-m2"}


@pytest.mark.asyncio
async def test_dreaming_merge_creates_vectorized_memory_and_archives_sources():
    memories = [
        memory("m1", content="Alice likes green tea", source_id="source-1", created_offset=2),
        memory("m2", content="Alice prefers jasmine tea", source_id="source-2", created_offset=1),
    ]
    action = ConsolidationAction(
        merges=[
            ConsolidationMerge(
                source_memory_ids=["m1", "m2"],
                target_content="Alice prefers green or jasmine tea.",
                target_entity_id="entity-1",
                target_property_name="preference",
                merge_reason="fragments describe the same preference",
            )
        ]
    )
    pipe, reader, writer = pipeline(memories=memories, action=action)

    await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert [item[0] for item in writer.deleted] == ["m1", "m2"]
    assert len(writer.plans) == 1
    plan = writer.plans[0]
    assert len(plan.memories) == 1
    assert plan.memories[0].content == "Alice prefers green or jasmine tea."
    assert plan.memories[0].parent_ids == ["m1", "m2"]
    assert plan.memories[0].metadata["source_ids"] == ["source-1", "source-2"]
    assert plan.memories[0].metadata["evidence_memory_ids"] == ["m1", "m2"]
    assert len(plan.vectors) == 1
    assert plan.vectors[0].semantic_vector == [0.1, 0.2, 0.3]
    assert {rel.target.node_id for rel in plan.relationships if rel.rel_type == "DERIVED_FROM"} == {"m1", "m2"}
    assert {rel.target.node_id for rel in plan.relationships if rel.rel_type == "EXTRACTED_FROM"} == {
        "source-1",
        "source-2",
    }


@pytest.mark.asyncio
async def test_dreaming_invalid_merge_never_archives_partial_sources():
    memories = [
        memory("m1", content="Alice likes green tea", created_offset=3),
        memory("m2", content="Alice prefers jasmine tea", created_offset=2),
        memory("m3", content="Alice also likes oolong tea", created_offset=1),
    ]
    action = ConsolidationAction(
        merges=[
            ConsolidationMerge(
                source_memory_ids=["m1", "m2", "missing-memory"],
                target_content="Alice likes tea.",
                merge_reason="invalid partial merge",
            )
        ]
    )
    pipe, _reader, writer = pipeline(memories=memories, action=action)

    result = await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert writer.plans == []
    assert writer.deleted == []
    assert "'actions': 0" in result.message


@pytest.mark.asyncio
async def test_dreaming_link_actions_cannot_reference_new_memories():
    memories = [
        memory("m1", content="Alice likes green tea", created_offset=2),
        memory("m2", content="Alice prefers jasmine tea", created_offset=1),
    ]
    action = ConsolidationAction(
        creates=[
            ConsolidationCreate(
                content="Alice has tea preferences.",
                evidence_memory_ids=["m1", "m2"],
                reason="generalized preference",
            )
        ],
        links=[
            ConsolidationLink(
                source_kind="Memory",
                source_id="new-memory",
                target_kind="Memory",
                target_id="m1",
                relation_type="generalizes",
            )
        ],
    )
    pipe, _reader, writer = pipeline(memories=memories, action=action)

    await pipe.dream_sync(DreamingPipelineInput(), ctx())

    plan = writer.plans[0]
    assert len(plan.memories) == 1
    assert all(rel.relation_type != "generalizes" for rel in plan.relationships)
    evidence_links = [rel for rel in plan.relationships if rel.rel_type == "RELATES_TO"]
    assert {rel.relation_type for rel in evidence_links if rel.target.node_id in {"m1", "m2"}} == {"dreaming_evidence"}
    assert {rel.target.node_id for rel in plan.relationships if rel.rel_type == "DERIVED_FROM"} == {"m1", "m2"}


@pytest.mark.asyncio
async def test_dreaming_rejects_create_without_valid_evidence_and_does_not_count_it():
    memories = [
        memory("m1", content="Alice likes green tea", created_offset=2),
        memory("m2", content="Alice prefers jasmine tea", created_offset=1),
    ]
    action = ConsolidationAction(
        creates=[
            ConsolidationCreate(
                content="Alice has tea preferences.",
                evidence_memory_ids=[],
                reason="unsupported generalization",
            )
        ]
    )
    pipe, _reader, writer = pipeline(memories=memories, action=action)

    result = await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert writer.plans == []
    assert "'actions': 0" in result.message


@pytest.mark.asyncio
async def test_dreaming_create_carries_source_and_memory_lineage():
    memories = [
        memory("m1", content="Alice likes green tea", source_id="source-1", created_offset=2),
        memory("m2", content="Alice prefers jasmine tea", source_id="source-2", created_offset=1),
    ]
    action = ConsolidationAction(
        creates=[
            ConsolidationCreate(
                content="Alice has tea preferences.",
                evidence_memory_ids=["m1", "m2"],
                reason="supported generalization",
            )
        ]
    )
    pipe, _reader, writer = pipeline(memories=memories, action=action)

    result = await pipe.dream_sync(DreamingPipelineInput(), ctx())

    assert "'actions': 1" in result.message
    plan = writer.plans[0]
    created = plan.memories[0]
    assert created.metadata["evidence_memory_ids"] == ["m1", "m2"]
    assert created.metadata["source_ids"] == ["source-1", "source-2"]
    derived = [rel for rel in plan.relationships if rel.rel_type == "DERIVED_FROM"]
    extracted = [rel for rel in plan.relationships if rel.rel_type == "EXTRACTED_FROM"]
    assert {rel.target.node_id for rel in derived} == {"m1", "m2"}
    assert {rel.target.node_id for rel in extracted} == {"source-1", "source-2"}


@pytest.mark.asyncio
async def test_dreaming_filters_structural_noise_and_collapses_entity_aliases():
    memories = [
        memory("m1", content="GitLab project one"),
        memory("m2", content="GitLab project two"),
        memory("m3", content="GitLab project three"),
        memory("m4", content="A numbered list item"),
    ]
    pipe, reader, _writer = pipeline(memories=memories, action=ConsolidationAction())
    reader.neo4j_rows = [
        {"entity_id": "gitlab-1", "entity_name": "GitLab", "entity_type": "product", "memory_ids": ["m1", "m2"]},
        {"entity_id": "gitlab-2", "entity_name": "git lab", "entity_type": "product", "memory_ids": ["m2", "m3"]},
        {"entity_id": "noise-1", "entity_name": "200", "entity_type": "number", "memory_ids": ["m1", "m4"]},
        {"entity_id": "noise-2", "entity_name": "第一", "entity_type": "ordinal", "memory_ids": ["m2", "m4"]},
    ]

    clusters = await pipe._cluster_hot_memories(ctx())

    assert len(clusters) == 1
    scope, clustered = clusters[0]
    assert scope.graph_entity_name == "GitLab"
    assert set(scope.seed_memory_ids) == {"m1", "m2", "m3"}
    assert {item.memory_id for item in clustered} == {"m1", "m2", "m3"}
