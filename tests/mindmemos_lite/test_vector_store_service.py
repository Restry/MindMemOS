from collections.abc import Mapping
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mindmemos_lite.components.text import SparseVectorEncoder, TextPreprocessor
from mindmemos_lite.config import MemoryConfig, TextProcessingConfig, VanillaAddConfig, VanillaSearchConfig
from mindmemos_lite.errors import MemoryUpdateError
from mindmemos_lite.infra.tasking import (
    TaskBackendHealth,
    TaskClient,
    TaskHandlerRegistry,
    TaskReceipt,
)
from mindmemos_lite.infra.vector_store import (
    BackendCapabilities,
    DatabaseScope,
    GraphEdge,
    GraphNode,
    GraphNodeRef,
    GraphSort,
    GraphStep,
    GraphTraversalQuery,
    Predicate,
    Record,
    ScopedVectorStore,
    SparseVector,
    TableRegistry,
    TableSpec,
    VectorDBService,
    VectorHit,
    VectorValue,
)
from mindmemos_lite.persistence import MemoryPersistence
from mindmemos_lite.persistence.memory import _memory_view
from mindmemos_lite.persistence.v2 import ADD_RECORD_TABLE, SEARCH_RECORD_TABLE
from mindmemos_lite.pipeline.vanilla_memory import VanillaAddPipeline, VanillaSearchPipeline
from mindmemos_lite.pipeline.vanilla_memory.search import VanillaSearchEngine
from mindmemos_lite.service.memory import VanillaMemoryService
from mindmemos_lite.service.schema import (
    AddMemoryRequest,
    RequestContext,
    SearchMemoryRequest,
    TextMessage,
)
from mindmemos_lite.typing import (
    MemoryDbMemoryUpdateCommand,
    MemoryDbSearchQuery,
    MemoryRequestContext,
    SearchPipelineInput,
)


class _MemoryVectorStore(ScopedVectorStore):
    name = "memory"
    capabilities = BackendCapabilities()

    def __init__(self) -> None:
        self.records: dict[tuple[str, DatabaseScope, str], Record] = {}
        self.schemas: list[TableRegistry] = []
        self.vector_queries = []

    async def ensure_schema(self, tables: TableRegistry) -> None:
        self.schemas.append(tables)

    async def upsert_records(self, table, records) -> None:
        for record in records:
            self.records[(table, record.scope, record.record_id)] = record

    async def get_records(self, table, scope, record_ids, *, with_vectors=False):
        return [
            record for record_id in record_ids if (record := self.records.get((table, scope, record_id))) is not None
        ]

    async def patch_record(self, table, scope, record_id, changes) -> None:
        record = self.records[(table, scope, record_id)]
        self.records[(table, scope, record_id)] = Record(
            table=record.table,
            record_id=record.record_id,
            scope=record.scope,
            payload={**record.payload, **changes},
            vectors=record.vectors,
        )

    async def delete_records(self, table, scope, record_ids) -> None:
        for record_id in record_ids:
            self.records.pop((table, scope, record_id), None)

    async def query_records(self, table, query):
        records = [
            record
            for (record_table, record_scope, _), record in self.records.items()
            if record_table == table and query.scope.matches(record_scope) and _matches(query.filters, record.payload)
        ]
        return records[: query.page.limit], None

    async def scroll(self, table, query, *, with_vectors=False):
        return await self.query_records(table, query)

    async def search_vectors(self, query):
        self.vector_queries.append(query)
        records = [
            record
            for (record_table, record_scope, _), record in self.records.items()
            if record_table == query.table
            and query.scope.matches(record_scope)
            and _matches(query.filters, record.payload)
        ]
        query_sparse = dict(zip(query.sparse_indices or (), query.sparse_values or (), strict=True))
        hits = []
        for record in records:
            sparse = (record.vectors.sparse if record.vectors is not None else {}).get("bm25")
            score = (
                sum(query_sparse.get(index, 0.0) * value for index, value in zip(sparse.indices, sparse.values))
                if sparse is not None
                else 0.0
            )
            if score > 0:
                hits.append(VectorHit(record=record, score=score, source="memory_sparse"))
        return sorted(hits, key=lambda hit: (-hit.score, hit.record.record_id))[: query.top_k]

    async def close(self) -> None:
        return None


def _matches(expression, values: Mapping[str, object]) -> bool:
    if expression is None:
        return True
    if isinstance(expression, Predicate):
        actual = values.get(expression.field)
        if expression.op == "eq":
            return actual == expression.value
        if expression.op == "in":
            return actual in expression.value
        if expression.op == "not_in":
            return actual not in expression.value
        if expression.op == "icontains":
            return str(expression.value).lower() in str(actual).lower()
        if expression.op == "is_null":
            return (actual is None) is bool(expression.value)
        if expression.op == "is_empty":
            return actual is None or actual == "" or actual == [] or actual == {}
        if expression.op in {"gt", "gte", "lt", "lte"}:
            return {
                "gt": actual > expression.value,
                "gte": actual >= expression.value,
                "lt": actual < expression.value,
                "lte": actual <= expression.value,
            }[expression.op]
        raise AssertionError(f"test backend does not implement {expression.op}")
    if expression.operator == "and":
        return all(_matches(clause, values) for clause in expression.clauses)
    if expression.operator == "or":
        return any(_matches(clause, values) for clause in expression.clauses)
    return not any(_matches(clause, values) for clause in expression.clauses)


class _NoEntities:
    def extract(self, _text, _lang):
        return []

    def extract_many(self, texts, _langs):
        return [[] for _ in texts]


class _NoEmbedding:
    async def embed(self, **_kwargs):
        return SimpleNamespace(embeddings=[])


class _DenseEmbedding:
    async def embed(self, **_kwargs):
        return SimpleNamespace(embeddings=[[1.0, 0.0, 0.0]])


class _TrackingEmbedding:
    def __init__(self, embeddings):
        self.embeddings = embeddings
        self.calls = []

    async def embed(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(embeddings=self.embeddings)


class _ImmediateTaskBackend:
    name = "immediate"

    def __init__(self, handlers: TaskHandlerRegistry) -> None:
        self.handlers = handlers

    async def start(self) -> None:
        return None

    async def submit(self, task):
        await self.handlers.resolve(task.task_name)(task)
        return TaskReceipt(task_id=task.task_id, task_name=task.task_name)

    async def flush(self, timeout=None) -> None:
        del timeout

    async def health(self) -> TaskBackendHealth:
        return TaskBackendHealth(
            backend=self.name,
            state="running",
            accepting=True,
            queue_depth=0,
            capacity=1,
            in_flight=0,
            failed_count=0,
        )

    async def close(self, timeout=None) -> None:
        del timeout


def test_memory_persistence_normalizes_pgvector_uuid_values_to_algorithm_dtos() -> None:
    memory_id = uuid4()
    request_id = uuid4()
    parent_id = uuid4()
    record = Record(
        table="memory_item_v2",
        record_id=str(memory_id),
        scope=DatabaseScope(
            account_id="account",
            project_id="project",
            api_key_uuid=str(uuid4()),
        ),
        payload={
            "memory_id": memory_id,
            "request_id": request_id,
            "content": "coffee",
            "mem_type": "fact",
            "mem_extract_type": "vanilla",
            "mem_extract_version": "v1",
            "metadata": {},
            "status": "active",
            "reinforcement_count": 0,
            "created_at": "2026-07-24T00:00:00+00:00",
            "parent_ids": [parent_id],
            "root_id": [memory_id],
            "schema_version": 2,
        },
    )

    memory = _memory_view(record)

    assert memory.memory_id == str(memory_id)
    assert memory.request_id == str(request_id)
    assert memory.parent_ids == [str(parent_id)]
    assert memory.root_id == [str(memory_id)]


@pytest.mark.asyncio
async def test_memory_persistence_reads_by_original_project_scope_not_actor_scope() -> None:
    memory_id = str(uuid4())
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(VectorDBService(backend))
    await backend.upsert_records(
        "memory_item_v2",
        [
            Record(
                table="memory_item_v2",
                record_id=memory_id,
                scope=DatabaseScope(
                    account_id="account",
                    project_id="project",
                    api_key_uuid=str(uuid4()),
                    user_id="writer",
                    session_id="writer-session",
                ),
                payload={
                    "memory_id": memory_id,
                    "content": "shared project memory",
                    "mem_type": "fact",
                    "mem_extract_type": "vanilla",
                    "mem_extract_version": "v1",
                    "metadata": {},
                    "status": "active",
                    "created_at": "2026-07-24T00:00:00+00:00",
                },
            )
        ],
    )
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
        user_id="reader",
        session_id="reader-session",
    )

    memory = await persistence.get_memory(context, memory_id)

    assert memory is not None
    assert memory.memory_id == memory_id
    assert memory.user_id == "writer"


@pytest.mark.asyncio
async def test_memory_persistence_preserves_hybrid_channel_limits() -> None:
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(VectorDBService(backend))
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
    )

    await persistence.search_hybrid(
        context,
        MemoryDbSearchQuery(query="coffee", top_k=5),
        dense_vector=[1.0, 0.0, 0.0],
        sparse_vector=SparseVector(indices=(1,), values=(2.0,)),
        dense_limit=30,
        sparse_limit=40,
    )

    vector_query = backend.vector_queries[-1]
    assert vector_query.top_k == 5
    assert vector_query.dense_limit == 30
    assert vector_query.sparse_limit == 40


@pytest.mark.asyncio
async def test_memory_persistence_content_update_refreshes_text_metadata_and_vectors() -> None:
    memory_id = str(uuid4())
    scope = DatabaseScope(account_id="account", project_id="project", api_key_uuid=str(uuid4()))
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
    )
    text_config = TextProcessingConfig(bm25_use_spacy_lemma=False)
    preprocessor = TextPreprocessor(text_config, entity_extractor=_NoEntities())
    sparse_encoder = SparseVectorEncoder(text_config)
    embed_client = _TrackingEmbedding([[0.9, 0.8, 0.7]])
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(
        VectorDBService(backend),
        text_config=text_config,
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        embed_client=embed_client,
    )
    await backend.upsert_records(
        "memory_item_v2",
        [
            Record(
                table="memory_item_v2",
                record_id=memory_id,
                scope=scope,
                payload={
                    "memory_id": memory_id,
                    "content": "old coffee content",
                    "mem_type": "fact",
                    "metadata": {
                        "source": "test",
                        "content_hash": "old-hash",
                        "bm25_text": "old coffee content",
                        "tokens": ["old", "coffee", "content"],
                        "lang": "en",
                    },
                    "status": "active",
                },
                vectors=VectorValue(
                    dense={"semantic": (1.0, 0.0, 0.0)},
                    sparse={"bm25": SparseVector(indices=(999,), values=(1.0,))},
                ),
            )
        ],
    )

    result = await persistence.update_memory(
        context,
        MemoryDbMemoryUpdateCommand(memory_id=memory_id, content="  Brand   NEW content  "),
    )

    expected_text = preprocessor.preprocess_text(
        "  Brand   NEW content  ",
        segment_id="update",
        include_entities=False,
    )
    expected_sparse = sparse_encoder.encode_document(expected_text.tokens)
    updated = backend.records[("memory_item_v2", scope, memory_id)]
    assert result.changed is True
    assert updated.payload["content"] == expected_text.normalized_text
    assert updated.payload["metadata"]["content_hash"] == expected_text.content_hash
    assert updated.payload["metadata"]["bm25_text"] == expected_text.bm25_text
    assert updated.payload["metadata"]["tokens"] == list(expected_text.tokens)
    assert updated.payload["metadata"]["lang"] == expected_text.lang
    assert updated.payload["metadata"]["source"] == "test"
    assert embed_client.calls == [{"task": "memory.update", "text": expected_text.normalized_text}]
    assert updated.vectors.dense["semantic"] == (0.9, 0.8, 0.7)
    assert updated.vectors.sparse["bm25"] == SparseVector(
        indices=tuple(expected_sparse.indices),
        values=tuple(expected_sparse.values),
    )
    assert updated.vectors.sparse["bm25"].indices != (999,)

    search_result = await persistence.search_sparse(
        context,
        MemoryDbSearchQuery(query=expected_text.normalized_text, top_k=5),
        indices=list(expected_sparse.indices),
        values=list(expected_sparse.values),
    )
    assert [hit.memory.memory_id for hit in search_result.hits] == [memory_id]


@pytest.mark.asyncio
async def test_memory_persistence_content_update_reuses_precomputed_vectors() -> None:
    memory_id = str(uuid4())
    scope = DatabaseScope(project_id="project")
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
    )
    text_config = TextProcessingConfig(bm25_use_spacy_lemma=False)
    embed_client = _TrackingEmbedding([[0.1, 0.2, 0.3]])
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(
        VectorDBService(backend),
        text_config=text_config,
        text_preprocessor=TextPreprocessor(text_config, entity_extractor=_NoEntities()),
        sparse_encoder=SparseVectorEncoder(text_config),
        embed_client=embed_client,
    )
    await backend.upsert_records(
        "memory_item_v2",
        [
            Record(
                table="memory_item_v2",
                record_id=memory_id,
                scope=scope,
                payload={"memory_id": memory_id, "content": "old", "metadata": {}, "status": "active"},
                vectors=VectorValue(
                    dense={"semantic": (1.0,)},
                    sparse={"bm25": SparseVector(indices=(1,), values=(1.0,))},
                ),
            )
        ],
    )

    await persistence.update_memory(
        context,
        MemoryDbMemoryUpdateCommand(
            memory_id=memory_id,
            content="new content",
            dense_vector=[0.1, 0.2],
            embedding=[0.9, 0.8],
            bm25_indices=[10, 11],
        ),
    )

    updated = backend.records[("memory_item_v2", scope, memory_id)]
    assert embed_client.calls == []
    assert updated.vectors.dense["semantic"] == (0.9, 0.8)
    assert updated.vectors.sparse["bm25"] == SparseVector(
        indices=(10, 11),
        values=(1.0, 1.0),
    )
    assert updated.payload["metadata"]["content_hash"]
    assert updated.payload["metadata"]["bm25_text"] == "new content"


@pytest.mark.asyncio
async def test_memory_persistence_content_update_is_atomic_when_embedding_is_empty() -> None:
    memory_id = str(uuid4())
    scope = DatabaseScope(project_id="project")
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
    )
    text_config = TextProcessingConfig(bm25_use_spacy_lemma=False)
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(
        VectorDBService(backend),
        text_config=text_config,
        text_preprocessor=TextPreprocessor(text_config, entity_extractor=_NoEntities()),
        sparse_encoder=SparseVectorEncoder(text_config),
        embed_client=_TrackingEmbedding([]),
    )
    original = Record(
        table="memory_item_v2",
        record_id=memory_id,
        scope=scope,
        payload={"memory_id": memory_id, "content": "old content", "metadata": {}, "status": "active"},
        vectors=VectorValue(
            dense={"semantic": (1.0,)},
            sparse={"bm25": SparseVector(indices=(1,), values=(1.0,))},
        ),
    )
    await backend.upsert_records("memory_item_v2", [original])

    with pytest.raises(MemoryUpdateError, match="empty vector"):
        await persistence.update_memory(
            context,
            MemoryDbMemoryUpdateCommand(memory_id=memory_id, content="new content"),
        )

    assert backend.records[("memory_item_v2", scope, memory_id)] is original


@pytest.mark.asyncio
async def test_vanilla_search_owns_hybrid_prefetch_calculation() -> None:
    backend = _MemoryVectorStore()
    persistence = MemoryPersistence(VectorDBService(backend))
    text_config = TextProcessingConfig(bm25_use_spacy_lemma=False)
    engine = VanillaSearchEngine(
        text_config=text_config,
        search_config=VanillaSearchConfig(recall_size=5, dedup_enabled=False),
        text_preprocessor=TextPreprocessor(text_config, entity_extractor=_NoEntities()),
        sparse_encoder=SparseVectorEncoder(text_config),
        embed_client=_DenseEmbedding(),
        persistence=persistence,
    )
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
    )

    result = await engine.search_candidates(
        SearchPipelineInput(query="coffee", top_k=5, search_pipeline="vanilla"),
        context,
    )

    assert result == []
    vector_query = backend.vector_queries[-1]
    assert vector_query.top_k == 5
    assert vector_query.dense_limit == 30
    assert vector_query.sparse_limit == 30


@pytest.mark.asyncio
async def test_graph_enabled_service_bootstraps_graph_tables() -> None:
    backend = _MemoryVectorStore()
    service = VectorDBService(backend, graph_enabled=True)
    business_tables = TableRegistry((TableSpec(name="memory_item", primary_key="memory_id"),))

    await service.ensure_schema(business_tables)

    assert {spec.name for spec in backend.schemas[0].specs} == {
        "memory_item",
        "graph_node",
        "graph_edge",
    }
    graph_node = backend.schemas[0].get("graph_node")
    assert graph_node.primary_key == "graph_node_id"
    assert {field.name for field in graph_node.fields} == {"node_id", "node_type"}


@pytest.mark.asyncio
async def test_service_composes_shared_entity_retrieval() -> None:
    scope = DatabaseScope(project_id="project")
    memory_1 = GraphNodeRef(scope=scope, kind="Memory", node_id="m1")
    entity = GraphNodeRef(scope=scope, kind="Entity", node_id="e1")
    second_entity = GraphNodeRef(scope=scope, kind="Entity", node_id="e2")
    memory_2 = GraphNodeRef(scope=scope, kind="Memory", node_id="m2")
    backend = _MemoryVectorStore()
    service = VectorDBService(
        backend,
        graph_enabled=True,
        node_tables={"Memory": "memory_item"},
    )
    await service.upsert_records(
        "memory_item",
        [
            Record(table="memory_item", record_id="m1", scope=scope, payload={"status": "inactive"}),
            Record(table="memory_item", record_id="m2", scope=scope, payload={"status": "active"}),
        ],
    )
    await service.upsert_nodes(
        [
            GraphNode(ref=memory_1),
            GraphNode(ref=entity),
            GraphNode(ref=second_entity),
            GraphNode(ref=memory_2),
        ]
    )
    await service.upsert_edges(
        [
            GraphEdge(source=memory_1, target=entity, relation="MENTIONS"),
            GraphEdge(source=memory_2, target=entity, relation="MENTIONS"),
            GraphEdge(source=memory_1, target=second_entity, relation="MENTIONS"),
            GraphEdge(source=memory_2, target=second_entity, relation="MENTIONS"),
        ]
    )

    result = await service.traverse(
        GraphTraversalQuery(
            scope=scope,
            seeds=(memory_1,),
            steps=(
                GraphStep(relations=("MENTIONS",), direction="out", target_kinds=("Entity",)),
                GraphStep(
                    relations=("MENTIONS",),
                    direction="in",
                    target_kinds=("Memory",),
                    target_filters=Predicate(field="status", op="eq", value="active"),
                ),
            ),
            result_uniqueness="end_node",
        )
    )

    assert [path.end.ref for path in result.paths] == [memory_2]
    assert [edge.direction for edge in result.paths[0].edges] == ["out", "in"]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_service_supports_bounded_variable_length_lineage() -> None:
    scope = DatabaseScope(project_id="project")
    child = GraphNodeRef(scope=scope, kind="Memory", node_id="child")
    parent = GraphNodeRef(scope=scope, kind="Memory", node_id="parent")
    ancestor = GraphNodeRef(scope=scope, kind="Memory", node_id="ancestor")
    service = VectorDBService(_MemoryVectorStore(), graph_enabled=True)
    await service.upsert_nodes([GraphNode(ref=child), GraphNode(ref=parent), GraphNode(ref=ancestor)])
    await service.upsert_edges(
        [
            GraphEdge(source=child, target=parent, relation="DERIVED_FROM"),
            GraphEdge(source=parent, target=ancestor, relation="DERIVED_FROM"),
        ]
    )

    result = await service.traverse(
        GraphTraversalQuery(
            scope=scope,
            seeds=(child,),
            steps=(
                GraphStep(
                    relations=("DERIVED_FROM",),
                    direction="out",
                    target_kinds=("Memory",),
                    min_hops=1,
                    max_hops=2,
                ),
            ),
        )
    )

    assert [path.end.ref for path in result.paths] == [parent, ancestor]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_memory_persistence_returns_transitive_lineage_and_shared_entity_metadata() -> None:
    graph_scope = DatabaseScope(project_id="project")
    record_scope = DatabaseScope(
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
        user_id="user",
    )
    backend = _MemoryVectorStore()
    database = VectorDBService(
        backend,
        graph_enabled=True,
        node_tables={"Memory": "memory_item_v2", "Entity": "entity_item_v2"},
    )
    persistence = MemoryPersistence(database)
    child = GraphNodeRef(scope=graph_scope, kind="Memory", node_id="child")
    parent = GraphNodeRef(scope=graph_scope, kind="Memory", node_id="parent")
    ancestor = GraphNodeRef(scope=graph_scope, kind="Memory", node_id="ancestor")
    peer = GraphNodeRef(scope=graph_scope, kind="Memory", node_id="peer")
    entity = GraphNodeRef(scope=graph_scope, kind="Entity", node_id="entity")
    await database.upsert_records(
        "memory_item_v2",
        [
            Record(
                table="memory_item_v2",
                record_id=memory_id,
                scope=record_scope,
                payload={
                    "memory_id": memory_id,
                    "content": memory_id,
                    "mem_type": "fact",
                    "mem_extract_type": "vanilla",
                    "mem_extract_version": "v1",
                    "metadata": {},
                    "status": "active",
                    "created_at": "2026-07-24T00:00:00+00:00",
                },
            )
            for memory_id in ("child", "parent", "ancestor", "peer")
        ],
    )
    await database.upsert_records(
        "entity_item_v2",
        [
            Record(
                table="entity_item_v2",
                record_id="entity",
                scope=record_scope,
                payload={
                    "entity_id": "entity",
                    "entity_name": "Coffee",
                    "entity_type": "preference",
                    "status": "active",
                    "created_at": "2026-07-24T00:00:00+00:00",
                },
            )
        ],
    )
    await database.upsert_nodes(
        [
            GraphNode(ref=child),
            GraphNode(ref=parent),
            GraphNode(ref=ancestor),
            GraphNode(ref=peer),
            GraphNode(ref=entity),
        ]
    )
    await database.upsert_edges(
        [
            GraphEdge(source=child, target=parent, relation="DERIVED_FROM"),
            GraphEdge(source=parent, target=ancestor, relation="DERIVED_FROM"),
            GraphEdge(source=child, target=entity, relation="MENTIONS"),
            GraphEdge(source=peer, target=entity, relation="MENTIONS"),
        ]
    )
    context = MemoryRequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
        user_id="reader",
    )

    lineage = await persistence.get_memory_lineage(context, ["child"])
    scopes = await persistence.list_memories_by_shared_entities(
        context,
        ["child"],
        include_seed=False,
    )
    clusters = await persistence.list_entity_memory_clusters(
        context,
        ["child"],
        limit_per_entity=3,
    )

    assert lineage == {"child": ["parent", "ancestor"]}
    assert len(scopes) == 1
    assert scopes[0].entity_name == "Coffee"
    assert scopes[0].entity_type == "preference"
    assert scopes[0].memory_ids == ("peer",)
    assert len(clusters) == 1
    assert clusters[0].entity_id == "entity"
    assert clusters[0].memory_ids == ("child", "peer")


@pytest.mark.asyncio
async def test_graph_sort_honors_null_placement_for_descending_order() -> None:
    scope = DatabaseScope(project_id="project")
    seed = GraphNodeRef(scope=scope, kind="Memory", node_id="seed")
    ranked = GraphNodeRef(scope=scope, kind="Memory", node_id="ranked")
    missing = GraphNodeRef(scope=scope, kind="Memory", node_id="missing")
    service = VectorDBService(_MemoryVectorStore(), graph_enabled=True)
    await service.upsert_nodes([GraphNode(ref=seed), GraphNode(ref=ranked), GraphNode(ref=missing)])
    await service.upsert_edges(
        [
            GraphEdge(source=seed, target=missing, relation="RELATES_TO"),
            GraphEdge(source=seed, target=ranked, relation="RELATES_TO", properties={"rank": 2}),
        ]
    )

    result = await service.traverse(
        GraphTraversalQuery(
            scope=scope,
            seeds=(seed,),
            steps=(GraphStep(relations=("RELATES_TO",), direction="out"),),
            order_by=(
                GraphSort(
                    scope="last_edge",
                    field="properties.rank",
                    direction="desc",
                    nulls="last",
                ),
            ),
        )
    )

    assert [path.end.ref for path in result.paths] == [ranked, missing]


@pytest.mark.asyncio
async def test_vanilla_add_then_search_runs_through_lite_persistence() -> None:
    backend = _MemoryVectorStore()
    database = VectorDBService(backend, graph_enabled=False)
    persistence = MemoryPersistence(database)
    text_config = TextProcessingConfig(bm25_use_spacy_lemma=False)
    preprocessor = TextPreprocessor(text_config, entity_extractor=_NoEntities())
    sparse_encoder = SparseVectorEncoder(text_config)
    add_pipeline = VanillaAddPipeline(
        text_config=text_config,
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        vanilla_add_config=VanillaAddConfig(enable_entities=False),
        consistency="fast",
        llm_client=None,
        embed_client=None,
        persistence=persistence,
    )
    search_engine = VanillaSearchEngine(
        text_config=text_config,
        search_config=VanillaSearchConfig(recall_size=5, dedup_enabled=False),
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        embed_client=_NoEmbedding(),
        persistence=persistence,
    )
    search_pipeline = VanillaSearchPipeline(
        engine=search_engine,
        rerank_client=None,
        persistence=persistence,
    )
    service = VanillaMemoryService(
        persistence,
        config=MemoryConfig(),
        add_pipeline=add_pipeline,
        search_pipeline=search_pipeline,
    )
    context = RequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
        user_id="user",
    )

    added = await service.add(
        context,
        AddMemoryRequest(messages=(TextMessage(text="I like coffee"),)),
    )
    found = await service.search(
        context,
        SearchMemoryRequest(query="coffee", top_k=5),
    )

    assert added.status == "ok"
    assert len(added.memories) == 1
    assert found.status == "ok"
    assert [(item.memory_id, item.content) for item in found.memories] == [
        (added.memories[0].memory_id, "I like coffee")
    ]
    add_records = [record for (table, _, _), record in backend.records.items() if table == ADD_RECORD_TABLE]
    search_records = [record for (table, _, _), record in backend.records.items() if table == SEARCH_RECORD_TABLE]
    assert [record.payload["status"] for record in add_records] == ["ok"]
    assert [record.payload["query"] for record in search_records] == ["coffee"]
    assert not hasattr(add_pipeline, "recorder")
    assert not hasattr(add_pipeline, "add_async")

    handlers = TaskHandlerRegistry()
    task_client = TaskClient(_ImmediateTaskBackend(handlers), handlers)
    async_add_pipeline = VanillaAddPipeline(
        text_config=text_config,
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        vanilla_add_config=VanillaAddConfig(enable_entities=False),
        consistency="fast",
        llm_client=None,
        embed_client=None,
        persistence=persistence,
    )
    async_service = VanillaMemoryService(
        persistence,
        config=MemoryConfig(),
        task_client=task_client,
        add_pipeline=async_add_pipeline,
        direct_add_pipeline=async_add_pipeline,
        search_pipeline=search_pipeline,
    )

    queued = await async_service.add(
        context,
        AddMemoryRequest(
            messages=(
                TextMessage(text="I enjoy tea"),
                TextMessage(text="It helps me focus"),
            ),
            mode="async",
            infer=False,
        ),
    )
    tea = await async_service.search(
        context,
        SearchMemoryRequest(query="tea", top_k=5),
    )

    assert queued.status == "queued"
    assert [item.content for item in tea.memories] == ["I enjoy tea It helps me focus"]
    async_add_records = [
        record
        for (table, _, _), record in backend.records.items()
        if table == ADD_RECORD_TABLE and record.payload["mode"] == "async"
    ]
    assert len(async_add_records) == 1
    assert async_add_records[0].payload["status"] == "ok"
