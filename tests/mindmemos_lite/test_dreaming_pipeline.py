from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from mindmemos.components.activity import RecentActivityCollector
from mindmemos.components.dreaming.action_planning import action_planning_parser
from mindmemos.config import DreamingConfig, TextProcessingConfig
from mindmemos.infra.tasking import InMemoryTaskBackend, TaskClient, TaskHandlerRegistry
from mindmemos.persistence.memory import EntityMemoryCluster
from mindmemos.pipeline.dreaming import MEMORY_DREAMING_TOPIC, DefaultDreamingPipeline
from mindmemos.service.memory import VanillaMemoryService
from mindmemos.service.schema import DreamingMemoryRequest, RequestContext
from mindmemos.typing import (
    ActivityRecordSnapshot,
    ActivityScope,
    ConsolidationAction,
    DreamingPipelineInput,
    DreamingPipelineResult,
    MemoryRequestContext,
    MemoryView,
    RecentActivityBundle,
    WrittenMemoryRef,
)
from mindmemos.typing.algo import ConsolidationLink, ConsolidationMerge


class _FakePersistence:
    def __init__(self, memories: list[MemoryView]) -> None:
        self.service = object()
        self.memories = memories
        self.plans: list[object] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[object] = []
        self.cluster_limits: list[int] = []

    async def list_entity_memory_clusters(self, _ctx, _seed_ids, *, limit_per_entity: int):
        self.cluster_limits.append(limit_per_entity)
        return [
            EntityMemoryCluster(
                entity_id="entity-1",
                entity_name="Alice",
                entity_type="person",
                memory_ids=tuple(memory.memory_id for memory in self.memories[:limit_per_entity]),
            )
        ]

    async def get_memories(self, _ctx, memory_ids: list[str]) -> list[MemoryView]:
        requested = set(memory_ids)
        return [memory for memory in self.memories if memory.memory_id in requested]

    async def apply_mutation_plan(self, _ctx, plan, *, consistency="fast"):
        write_plan = plan.to_write_plan()
        if write_plan.memories or write_plan.relationships:
            self.plans.append(write_plan)
        for command in plan.memory_updates:
            self.updated.append(command)
        for command in plan.memory_deletes:
            self.deleted.append((command.memory_id, command.reason))
        return SimpleNamespace(
            memory_ids=[memory.memory_id for memory in write_plan.memories],
            mutations=[],
            errors=[],
            graph_pending=False,
        )


class _FakeAddRecords:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}

    async def get_many(self, _ctx, add_record_ids):
        return [
            SimpleNamespace(
                add_record_id=add_record_id,
                consolidation_status=self.statuses.get(add_record_id, "pending"),
            )
            for add_record_id in add_record_ids
        ]


class _FakeRecorder:
    def __init__(self) -> None:
        self.add_records = _FakeAddRecords()
        self.patches: list[tuple[str, dict]] = []

    async def patch_add_record(self, _ctx, add_record_id, payload):
        self.patches.append((add_record_id, payload))
        return True


class _FakeActivityCollector:
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


class _FakeEmbed:
    async def embed(self, *, task: str, text):
        del task
        texts = text if isinstance(text, list) else [text]
        return SimpleNamespace(embeddings=[[0.1, 0.2, 0.3] for _ in texts])


class _FakeLLM:
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


def _context() -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="00000000-0000-0000-0000-000000000001",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        session_id="sess",
        scopes=["memory:write"],
    )


def _memory(
    memory_id: str,
    *,
    content: str,
    content_hash: str | None = None,
    created_offset: int = 0,
) -> MemoryView:
    now = datetime.now(UTC)
    metadata = {"content_hash": content_hash} if content_hash else {}
    return MemoryView(
        memory_id=memory_id,
        project_id="proj",
        content=content,
        mem_type="fact",
        status="active",
        metadata=metadata,
        root_id=["root-1"],
        entity_id="entity-1",
        entity_type="person",
        property_name="preference",
        created_at=now - timedelta(minutes=created_offset),
        update_at=now - timedelta(minutes=created_offset),
    )


def _pipeline(
    memories: list[MemoryView],
    action: ConsolidationAction,
) -> tuple[DefaultDreamingPipeline, _FakePersistence, _FakeRecorder]:
    persistence = _FakePersistence(memories)
    recorder = _FakeRecorder()
    pipeline = DefaultDreamingPipeline(
        persistence=persistence,
        operation_recorder=recorder,
        dreaming_config=DreamingConfig(
            lookback_days=7,
            max_scopes_per_run=5,
            max_seed_memories=20,
            max_memories_per_scope=20,
            min_scope_updates=1,
            min_cluster_size=2,
        ),
        text_config=TextProcessingConfig(),
        llm_client=_FakeLLM(action),
        embed_client=_FakeEmbed(),
        activity_collector=_FakeActivityCollector(memories),
        consistency="fast",
    )
    return pipeline, persistence, recorder


@pytest.mark.asyncio
async def test_dreaming_archives_exact_duplicates_before_llm_actions() -> None:
    memories = [
        _memory("m1", content="Alice likes tea", content_hash="same", created_offset=1),
        _memory("m2", content="Alice likes tea", content_hash="same", created_offset=2),
    ]
    pipeline, persistence, _recorder = _pipeline(memories, ConsolidationAction())

    result = await pipeline.dream_sync(DreamingPipelineInput(mode="sync"), _context())

    assert result.status == "ok"
    assert persistence.deleted == [("m2", "duplicate_of:m1")]
    assert persistence.plans == []


@pytest.mark.asyncio
async def test_dreaming_filters_done_add_records_and_uses_noise_probe_limit() -> None:
    memories = [
        _memory("m1", content="Alice likes tea", created_offset=1),
        _memory("m2", content="Alice likes coffee", created_offset=2),
    ]
    pipeline, persistence, recorder = _pipeline(memories, ConsolidationAction())
    recorder.add_records.statuses = {"add-m1": "done", "add-m2": "pending"}

    clusters = await pipeline._cluster_hot_memories(_context())

    assert len(clusters) == 1
    scope, _ = clusters[0]
    assert scope.primary_memory_id == "m2"
    assert scope.add_record_ids == ("add-m2",)
    assert persistence.cluster_limits == [pipeline._cfg.max_entity_memory_count + 1]


@pytest.mark.asyncio
async def test_dreaming_merge_keeps_source_algorithm_write_shape() -> None:
    memories = [
        _memory("m1", content="Alice likes green tea", created_offset=2),
        _memory("m2", content="Alice prefers jasmine tea", created_offset=1),
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
    pipeline, persistence, _recorder = _pipeline(memories, action)

    await pipeline.dream_sync(DreamingPipelineInput(mode="sync"), _context())

    assert [memory_id for memory_id, _ in persistence.deleted] == ["m1", "m2"]
    assert len(persistence.plans) == 1
    plan = persistence.plans[0]
    assert plan.memories[0].content == "Alice prefers green or jasmine tea."
    assert plan.memories[0].parent_ids == ["m1", "m2"]
    assert plan.vectors[0].semantic_vector == [0.1, 0.2, 0.3]
    assert all(relationship.relation_type != "generalizes" for relationship in plan.relationships)


def test_action_planning_parser_preserves_link_compatibility() -> None:
    action = action_planning_parser(
        """
        {
          "links": [{
            "source_kind": "Memory",
            "source_id": "current",
            "target_kind": "Memory",
            "target_id": "stale",
            "link_type": "supersedes"
          }]
        }
        """
    )

    assert action.links[0].relation_type == "supersedes"


@pytest.mark.asyncio
async def test_activity_collector_groups_backend_neutral_recorder_snapshots() -> None:
    now = datetime.now(UTC)

    class Store:
        async def list_activity_records(self, kind, scope, **_kwargs):
            assert scope.project_id == "proj"
            if kind == "search":
                return []
            return [
                ActivityRecordSnapshot(
                    record_id="add-1",
                    payload={
                        "add_record_id": "add-1",
                        "project_id": "proj",
                        "session_id": "sess",
                        "user_id": "user",
                        "status": "ok",
                        "request_submitted_at": now,
                        "messages": [{"role": "user", "content": "I like tea"}],
                        "memories": [{"memory_id": "m1", "content": "User likes tea", "operation": "add"}],
                    },
                )
            ]

    bundle = await RecentActivityCollector(Store()).collect(
        ActivityScope(project_id="proj", user_id="user"),
        window_end=now,
    )

    assert bundle.conversations[0].session_id == "sess"
    assert bundle.written_memories[0].memory_id == "m1"
    assert bundle.written_memories[0].add_record_ids == ["add-1"]


@pytest.mark.asyncio
async def test_memory_service_owns_sync_and_async_dreaming_dispatch() -> None:
    class HandlerRegistry:
        def __init__(self):
            self.handlers = {}

        def names(self):
            return tuple(self.handlers)

        def register(self, name, handler):
            self.handlers[name] = handler

    class TaskClient:
        def __init__(self):
            self.handlers = HandlerRegistry()
            self.submissions = []

        async def submit(self, name, payload, *, dispatch_key):
            self.submissions.append((name, payload, dispatch_key))

    class Dreaming:
        def __init__(self):
            self.calls = []

        async def dream_sync(self, payload, context):
            self.calls.append((payload, context))
            return DreamingPipelineResult(status="ok", message="consolidation complete")

    class Add:
        async def add_sync(self, payload, context):
            raise AssertionError((payload, context))

    class Search:
        async def search(self, payload, context):
            raise AssertionError((payload, context))

    recorder = _FakeRecorder()
    recorder.record_add_input = recorder.mark_add_failed = recorder.record_search = SimpleNamespace()
    task_client = TaskClient()
    dreaming = Dreaming()
    persistence = SimpleNamespace(service=object())
    service = VanillaMemoryService(
        persistence,
        config=SimpleNamespace(),
        task_client=task_client,
        add_pipeline=Add(),
        search_pipeline=Search(),
        dreaming_pipeline=dreaming,
        recorder=recorder,
    )
    context = RequestContext(
        request_id="req",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        scopes=("memory:write",),
    )

    sync_result = await service.dream(context, DreamingMemoryRequest(mode="sync"))
    async_result = await service.dream(context, DreamingMemoryRequest(mode="async"))

    assert sync_result.status == "ok"
    assert len(dreaming.calls) == 1
    assert async_result.status == "queued"
    assert task_client.submissions[0][0] == MEMORY_DREAMING_TOPIC
    assert MEMORY_DREAMING_TOPIC in task_client.handlers.names()


@pytest.mark.asyncio
async def test_default_in_memory_backend_executes_dreaming_task() -> None:
    handlers = TaskHandlerRegistry()
    handled = []

    async def handler(task):
        handled.append(task.payload["marker"])

    handlers.register(MEMORY_DREAMING_TOPIC, handler)
    backend = InMemoryTaskBackend(handlers, max_concurrency=1, max_buffered=4)
    client = TaskClient(backend, handlers)
    await backend.start()
    try:
        await client.submit(
            MEMORY_DREAMING_TOPIC,
            {"marker": "dream"},
            dispatch_key="project:user",
        )
        await backend.flush(timeout=1)
        health = await backend.health()
    finally:
        await backend.close(timeout=1)

    assert handled == ["dream"]
    assert health.queue_depth == 0
    assert health.failed_count == 0


@pytest.mark.asyncio
async def test_memory_service_async_dreaming_runs_through_default_backend() -> None:
    class Dreaming:
        def __init__(self):
            self.calls = []

        async def dream_sync(self, payload, context):
            self.calls.append((payload, context))
            return DreamingPipelineResult(status="ok", message="done")

    class Add:
        async def add_sync(self, payload, context):
            raise AssertionError((payload, context))

    class Search:
        async def search(self, payload, context):
            raise AssertionError((payload, context))

    handlers = TaskHandlerRegistry()
    backend = InMemoryTaskBackend(handlers, max_concurrency=1, max_buffered=4)
    task_client = TaskClient(backend, handlers)
    dreaming = Dreaming()
    recorder = _FakeRecorder()
    persistence = SimpleNamespace(service=object())
    service = VanillaMemoryService(
        persistence,
        config=SimpleNamespace(),
        task_client=task_client,
        add_pipeline=Add(),
        search_pipeline=Search(),
        dreaming_pipeline=dreaming,
        recorder=recorder,
    )
    context = RequestContext(
        request_id="req",
        account_id="acct",
        project_id="proj",
        api_key_uuid="key",
        user_id="user",
        scopes=("memory:write",),
    )

    await backend.start()
    try:
        result = await service.dream(context, DreamingMemoryRequest(mode="async"))
        await backend.flush(timeout=1)
    finally:
        await backend.close(timeout=1)

    assert result.status == "queued"
    assert len(dreaming.calls) == 1
    assert dreaming.calls[0][0].mode == "async"
    assert dreaming.calls[0][1].project_id == "proj"
