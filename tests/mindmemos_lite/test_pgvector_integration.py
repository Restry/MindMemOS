from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from mindmemos.components.text import SparseVectorEncoder, TextPreprocessor
from mindmemos.config import MemoryConfig, TextProcessingConfig, VanillaAddConfig, VanillaSearchConfig
from mindmemos.infra.vector_store import (
    DatabaseScope,
    FieldSpec,
    FieldType,
    GraphEdge,
    GraphNode,
    GraphNodeRef,
    GraphStep,
    GraphTraversalQuery,
    Page,
    Predicate,
    Record,
    RecordQuery,
    SparseVector,
    TableRegistry,
    TableSpec,
    VectorDBService,
    VectorFieldSpec,
    VectorQuery,
    VectorValue,
    with_graph_tables,
)
from mindmemos.infra.vector_store.vector_store_impl import PgVectorBackend, PgVectorOptions
from mindmemos.persistence import MemoryPersistence
from mindmemos.persistence.v2 import build_v2_registry
from mindmemos.pipeline.vanilla_memory import VanillaAddPipeline, VanillaSearchPipeline
from mindmemos.pipeline.vanilla_memory.search import VanillaSearchEngine
from mindmemos.service.memory import VanillaMemoryService
from mindmemos.service.schema import AddMemoryRequest, RequestContext, SearchMemoryRequest, TextMessage
from psycopg import sql


class _NoEntities:
    def extract(self, _text, _lang):
        return []

    def extract_many(self, texts, _langs):
        return [[] for _ in texts]


class _DeterministicEmbedding:
    async def embed(self, *, text, **_kwargs):
        count = len(text) if isinstance(text, list) else 1
        return SimpleNamespace(embeddings=[[1.0, 0.0, 0.0] for _ in range(count)])


@pytest.mark.asyncio
async def test_pgvector_backend_against_postgresql() -> None:
    dsn = os.getenv("MINDMEMOS_TEST_PGVECTOR_DSN")
    if not dsn:
        pytest.skip("set MINDMEMOS_TEST_PGVECTOR_DSN to run the PostgreSQL + pgvector integration test")

    schema = f"mindmemos_test_{uuid4().hex}"
    tables = TableRegistry(
        (
            TableSpec(
                name="items",
                primary_key="item_id",
                fields=(
                    FieldSpec(name="item_id", field_type=FieldType.TEXT, nullable=False),
                    FieldSpec(name="project_id", field_type=FieldType.TEXT, nullable=False),
                    FieldSpec(name="content", field_type=FieldType.TEXT, nullable=False),
                    FieldSpec(name="metadata", field_type=FieldType.JSON, nullable=False),
                ),
                vectors=(
                    VectorFieldSpec(name="semantic", dimensions=3),
                    VectorFieldSpec(name="bm25", dimensions=8, distance="dot", sparse=True),
                ),
            ),
        )
    )
    tables.freeze()
    backend = PgVectorBackend(
        options=PgVectorOptions(dsn=dsn, schema=schema, min_pool_size=0, max_pool_size=2),
        tables=with_graph_tables(tables),
    )
    service = VectorDBService(backend, graph_enabled=True)
    scope_a = DatabaseScope(project_id="project-a", user_id="user-a")
    scope_b = DatabaseScope(project_id="project-b", user_id="user-b")

    try:
        await service.ensure_schema(tables)
        await backend.upsert_records(
            "items",
            (
                Record(
                    table="items",
                    record_id="shared-id",
                    scope=scope_a,
                    payload={
                        "item_id": "shared-id",
                        "project_id": "project-a",
                        "content": "coffee",
                        "metadata": {"priority": 2},
                    },
                    vectors=VectorValue(
                        dense={"semantic": (1.0, 0.0, 0.0)},
                        sparse={"bm25": SparseVector(indices=(0, 2), values=(1.0, 0.5))},
                    ),
                ),
                Record(
                    table="items",
                    record_id="shared-id",
                    scope=scope_b,
                    payload={
                        "item_id": "shared-id",
                        "project_id": "project-b",
                        "content": "tea",
                        "metadata": {"priority": 1},
                    },
                    vectors=VectorValue(
                        dense={"semantic": (0.0, 1.0, 0.0)},
                        sparse={"bm25": SparseVector(indices=(1,), values=(1.0,))},
                    ),
                ),
            ),
        )

        records = await backend.get_records("items", scope_a, ("shared-id",), with_vectors=True)
        assert len(records) == 1
        assert records[0].payload["content"] == "coffee"
        assert records[0].vectors is not None
        assert records[0].vectors.dense["semantic"] == (1.0, 0.0, 0.0)

        queried, cursor = await backend.query_records(
            "items",
            RecordQuery(
                scope=DatabaseScope(project_id="project-a"),
                filters=Predicate(field="metadata.priority", op="gte", value=2),
            ),
        )
        assert [record.record_id for record in queried] == ["shared-id"]
        assert cursor is None

        first_page, cursor = await backend.scroll(
            "items",
            RecordQuery(
                scope=DatabaseScope(),
                page=Page(limit=1),
            ),
            with_vectors=True,
        )
        assert [record.record_id for record in first_page] == ["shared-id"]
        assert first_page[0].vectors is not None
        assert cursor is not None

        second_page, next_cursor = await backend.scroll(
            "items",
            RecordQuery(
                scope=DatabaseScope(),
                page=Page(limit=1, cursor=cursor),
            ),
            with_vectors=True,
        )
        assert second_page[0].payload["content"] in {"coffee", "tea"}
        assert second_page[0].payload["content"] != first_page[0].payload["content"]
        assert second_page[0].vectors is not None
        assert next_cursor is None

        dense = await backend.search_vectors(
            VectorQuery(
                table="items",
                scope=DatabaseScope(project_id="project-a"),
                vector_name="semantic",
                dense_vector=(1.0, 0.0, 0.0),
            )
        )
        assert dense[0].record.record_id == "shared-id"
        assert dense[0].score == pytest.approx(1.0)

        sparse = await backend.search_vectors(
            VectorQuery(
                table="items",
                scope=DatabaseScope(project_id="project-a"),
                vector_name="bm25",
                sparse_indices=(0,),
                sparse_values=(1.0,),
                mode="sparse",
            )
        )
        assert sparse[0].record.record_id == "shared-id"

        hybrid = await backend.search_vectors(
            VectorQuery(
                table="items",
                scope=DatabaseScope(project_id="project-a"),
                vector_name="semantic",
                dense_vector=(1.0, 0.0, 0.0),
                sparse_indices=(0,),
                sparse_values=(1.0,),
                mode="hybrid",
            )
        )
        assert hybrid[0].record.record_id == "shared-id"
        assert hybrid[0].source == "rrf"

        memory = GraphNodeRef(scope=scope_a, kind="Memory", node_id="shared-id")
        entity = GraphNodeRef(scope=scope_a, kind="Entity", node_id="coffee")
        await service.upsert_nodes([GraphNode(ref=memory), GraphNode(ref=entity)])
        await service.upsert_edges([GraphEdge(source=memory, target=entity, relation="MENTIONS")])
        traversal = await service.traverse(
            GraphTraversalQuery(
                scope=scope_a,
                seeds=(memory,),
                steps=(GraphStep(relations=("MENTIONS",), direction="out", target_kinds=("Entity",)),),
            )
        )
        assert [path.end.ref for path in traversal.paths] == [entity]
        assert traversal.truncated is False

        await backend.patch_record("items", scope_a, "shared-id", {"content": "espresso"})
        assert (await backend.get_records("items", scope_a, ("shared-id",)))[0].payload["content"] == "espresso"
        assert (await backend.get_records("items", scope_b, ("shared-id",)))[0].payload["content"] == "tea"

        await backend.delete_records("items", scope_a, ("shared-id",))
        assert await backend.get_records("items", scope_a, ("shared-id",)) == []
        assert len(await backend.get_records("items", scope_b, ("shared-id",))) == 1
    finally:
        await backend.close()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.asyncio
async def test_vanilla_service_add_search_against_postgresql() -> None:
    dsn = os.getenv("MINDMEMOS_TEST_PGVECTOR_DSN")
    if not dsn:
        pytest.skip("set MINDMEMOS_TEST_PGVECTOR_DSN to run the PostgreSQL + pgvector integration test")

    schema = f"mindmemos_vanilla_test_{uuid4().hex}"
    text_config = TextProcessingConfig(
        bm25_use_spacy_lemma=False,
        sparse_hash_dim=128,
    )
    tables = build_v2_registry(vector_dimensions=3, sparse_hash_dim=text_config.sparse_hash_dim)
    backend = PgVectorBackend(
        options=PgVectorOptions(dsn=dsn, schema=schema, min_pool_size=0, max_pool_size=2),
        tables=tables,
    )
    database = VectorDBService(backend, graph_enabled=False)
    persistence = MemoryPersistence(database)
    preprocessor = TextPreprocessor(text_config, entity_extractor=_NoEntities())
    sparse_encoder = SparseVectorEncoder(text_config)
    embedding = _DeterministicEmbedding()
    add_pipeline = VanillaAddPipeline(
        text_config=text_config,
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        vanilla_add_config=VanillaAddConfig(enable_entities=False),
        consistency="strong",
        llm_client=None,
        embed_client=embedding,
        persistence=persistence,
    )
    search_engine = VanillaSearchEngine(
        text_config=text_config,
        search_config=VanillaSearchConfig(recall_size=5, dedup_enabled=False),
        text_preprocessor=preprocessor,
        sparse_encoder=sparse_encoder,
        embed_client=embedding,
        persistence=persistence,
    )
    memory = VanillaMemoryService(
        persistence,
        config=MemoryConfig(),
        add_pipeline=add_pipeline,
        search_pipeline=VanillaSearchPipeline(
            engine=search_engine,
            rerank_client=None,
            persistence=persistence,
        ),
    )
    context = RequestContext(
        request_id=str(uuid4()),
        account_id="account",
        project_id="project",
        api_key_uuid=str(uuid4()),
        user_id="user",
    )

    try:
        await database.ensure_schema(tables)
        added = await memory.add(
            context,
            AddMemoryRequest(messages=(TextMessage(text="I like coffee"),)),
        )
        found = await memory.search(
            context,
            SearchMemoryRequest(query="coffee", top_k=5),
        )

        assert added.status == "ok"
        assert len(added.memories) == 1
        assert found.status == "ok"
        assert [(item.memory_id, item.content) for item in found.memories] == [
            (added.memories[0].memory_id, "I like coffee")
        ]
    finally:
        await backend.close()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
