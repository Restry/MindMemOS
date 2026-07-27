from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from mindmemos.infra.vector_store import (
    BackendCapabilities,
    DatabaseScope,
    FilterGroup,
    Predicate,
    Record,
    ScopedVectorStore,
    VectorDBService,
)
from mindmemos.persistence import AddRecordPersistence, MemoryOperationRecorder, SearchRecordPersistence
from mindmemos.persistence.v2 import ADD_RECORD_TABLE, SEARCH_RECORD_TABLE, build_v2_registry
from mindmemos.typing import (
    ActivityScope,
    AddPipelineInput,
    AddPipelineSyncResult,
    MemoryAddEventItem,
    MemoryRequestContext,
    SearchPipelineInput,
    SearchPipelineResult,
    TextMessage,
)


class _RecorderStore(ScopedVectorStore):
    name = "recorder"
    capabilities = BackendCapabilities()

    def __init__(self) -> None:
        self.records: dict[tuple[str, DatabaseScope, str], Record] = {}

    async def ensure_schema(self, tables) -> None:
        del tables

    async def upsert_records(self, table, records) -> None:
        for record in records:
            self.records[(table, record.scope, record.record_id)] = record

    async def get_records(self, table, scope, record_ids, *, with_vectors=False):
        del with_vectors
        return [
            record
            for record_id in record_ids
            if (record := self.records.get((table, scope, record_id))) is not None
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
            if record_table == table
            and query.scope.matches(record_scope)
            and _matches(query.filters, record.payload)
        ]
        return records[: query.page.limit], None

    async def scroll(self, table, query, *, with_vectors=False):
        del with_vectors
        return await self.query_records(table, query)

    async def search_vectors(self, query):
        del query
        return []

    async def close(self) -> None:
        return None


def _matches(expression, payload: Mapping[str, object]) -> bool:
    if expression is None:
        return True
    if isinstance(expression, Predicate):
        actual = payload.get(expression.field)
        if expression.op == "eq":
            return actual == expression.value
        if expression.op == "in":
            return actual in expression.value
        if expression.op == "gte":
            return actual >= expression.value
        if expression.op == "lte":
            return actual <= expression.value
        raise AssertionError(f"unsupported predicate {expression.op}")
    assert isinstance(expression, FilterGroup)
    if expression.operator == "and":
        return all(_matches(clause, payload) for clause in expression.clauses)
    if expression.operator == "or":
        return any(_matches(clause, payload) for clause in expression.clauses)
    return not any(_matches(clause, payload) for clause in expression.clauses)


def _context(*, user_id: str = "writer") -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="request",
        account_id="account",
        project_id="project",
        api_key_uuid="key",
        user_id=user_id,
        session_id="session",
    )


@pytest.mark.asyncio
async def test_add_record_persistence_owns_full_lifecycle_and_project_scoped_reads() -> None:
    backend = _RecorderStore()
    persistence = AddRecordPersistence(VectorDBService(backend))
    submitted_at = datetime(2026, 7, 25, tzinfo=UTC)
    inp = AddPipelineInput(
        messages=[TextMessage(text="remember coffee")],
        event_timestamp_ms=1_753_392_000_000,
        mode="async",
        metadata={"source": "test"},
    )

    await persistence.record_add_input(
        inp,
        ctx=_context(),
        request_submitted_at=submitted_at,
        add_record_id="add-1",
        status="queued",
    )
    assert await persistence.mark_add_processing(_context(user_id="other-reader"), "add-1")
    assert await persistence.mark_add_completed(
        _context(user_id="other-reader"),
        "add-1",
        AddPipelineSyncResult(
            status="ok",
            memories=[
                MemoryAddEventItem(
                    operation="add",
                    content="likes coffee",
                    memory_id="memory-1",
                )
            ],
        ),
    )
    assert await persistence.append_add_output(
        _context(),
        "add-1",
        [
            MemoryAddEventItem(
                operation="update",
                content="likes espresso",
                memory_id="memory-1",
            )
        ],
    )

    record = await persistence.get(_context(user_id="reader"), "add-1")

    assert record is not None
    assert record.project_id == "project"
    assert record.user_id == "writer"
    assert record.status == "ok"
    assert record.mode == "async"
    assert [item["operation"] for item in record.memories] == ["add", "update"]
    assert record.metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_add_record_failure_is_idempotent_for_missing_or_out_of_project_records() -> None:
    backend = _RecorderStore()
    persistence = AddRecordPersistence(VectorDBService(backend))

    assert not await persistence.mark_add_failed(_context(), "missing", "boom")

    other_project = _context().model_copy(update={"project_id": "other-project"})
    await persistence.record_add_input(
        AddPipelineInput(messages=[TextMessage(text="hello")]),
        ctx=other_project,
        request_submitted_at=datetime.now(UTC),
        add_record_id="other-add",
        status="processing",
    )

    assert not await persistence.mark_add_failed(_context(), "other-add", "boom")


@pytest.mark.asyncio
async def test_search_record_persistence_preserves_query_result_snapshot_and_failure() -> None:
    backend = _RecorderStore()
    persistence = SearchRecordPersistence(VectorDBService(backend))
    submitted_at = datetime(2026, 7, 25, tzinfo=UTC)
    inp = SearchPipelineInput(
        query="coffee",
        filters={"user_id": "writer"},
        top_k=None,
        search_pipeline="vanilla",
        rerank=True,
        score_threshold=0.7,
    )

    success_id = await persistence.record_search(
        inp,
        SearchPipelineResult(status="ok", memories=[]),
        ctx=_context(),
        request_submitted_at=submitted_at,
        task_completed_at=submitted_at,
        search_record_id="search-ok",
    )
    failure_id = await persistence.record_search(
        inp,
        None,
        ctx=_context(),
        request_submitted_at=submitted_at,
        task_completed_at=submitted_at,
        search_record_id="search-error",
        error="embedding failed",
    )

    success = await persistence.get(_context(user_id="reader"), success_id)
    failure = await persistence.get(_context(), failure_id)

    assert success is not None
    assert success.status == "ok"
    assert success.query == "coffee"
    assert success.top_k is None
    assert success.score_threshold == 0.7
    assert success.filters == {"user_id": "writer"}
    assert failure is not None
    assert failure.status == "error"
    assert failure.error == "embedding failed"


def test_search_record_table_accepts_unbounded_top_k_and_failure_details() -> None:
    table = build_v2_registry(vector_dimensions=3, sparse_hash_dim=8).get(SEARCH_RECORD_TABLE)
    fields = {field.name: field for field in table.fields}

    assert fields["top_k"].nullable is True
    assert fields["score_threshold"].nullable is True
    assert fields["error"].nullable is True
    assert build_v2_registry(vector_dimensions=3, sparse_hash_dim=8).get(ADD_RECORD_TABLE).primary_key == (
        "add_record_id"
    )


@pytest.mark.asyncio
async def test_recorder_writes_only_declared_v2_payload_fields() -> None:
    backend = _RecorderStore()
    service = VectorDBService(backend)
    now = datetime.now(UTC)
    await AddRecordPersistence(service).record_add_input(
        AddPipelineInput(messages=[TextMessage(text="hello")]),
        ctx=_context(),
        request_submitted_at=now,
        add_record_id="add",
        status="processing",
    )
    await SearchRecordPersistence(service).record_search(
        SearchPipelineInput(query="hello", top_k=None, search_pipeline="vanilla"),
        SearchPipelineResult(status="ok", memories=[]),
        ctx=_context(),
        request_submitted_at=now,
        task_completed_at=now,
        search_record_id="search",
    )
    registry = build_v2_registry(vector_dimensions=3, sparse_hash_dim=8)

    for (table, _, _), record in backend.records.items():
        declared_fields = {field.name for field in registry.get(table).fields}
        required_fields = {
            field.name
            for field in registry.get(table).fields
            if not field.nullable and field.default is None
        }
        assert set(record.payload) <= declared_fields
        assert all(record.payload.get(field_name) is not None for field_name in required_fields)


@pytest.mark.asyncio
async def test_recorder_exposes_backend_neutral_recent_activity_and_dreaming_patch() -> None:
    backend = _RecorderStore()
    recorder = MemoryOperationRecorder.from_service(VectorDBService(backend))
    submitted_at = datetime(2026, 7, 25, 8, tzinfo=UTC)
    await recorder.record_add(
        AddPipelineInput(messages=[TextMessage(text="remember tea")]),
        AddPipelineSyncResult(
            status="ok",
            memories=[MemoryAddEventItem(operation="add", content="likes tea", memory_id="memory-1")],
        ),
        ctx=_context(),
        request_submitted_at=submitted_at,
        task_completed_at=submitted_at,
        add_record_id="add-activity",
    )

    snapshots = await recorder.list_activity_records(
        "add",
        ActivityScope(project_id="project", user_id="writer"),
        window_start=submitted_at - timedelta(minutes=1),
        window_end=submitted_at + timedelta(minutes=1),
        max_records=10,
        include_non_ok=False,
    )
    assert [snapshot.record_id for snapshot in snapshots] == ["add-activity"]
    assert snapshots[0].payload["user_id"] == "writer"
    assert snapshots[0].payload["memories"][0]["memory_id"] == "memory-1"

    await recorder.patch_add_record(
        _context(),
        "add-activity",
        {
            "feedback_processed": True,
            "consolidation_status": "done",
            "consolidated_at": submitted_at,
            "consolidation_run_id": "run-1",
        },
    )
    patched = await recorder.add_records.get(_context(), "add-activity")
    assert patched is not None
    assert patched.feedback_processed is True
    assert patched.consolidation_status == "done"
    assert patched.consolidation_run_id == "run-1"

    pending = await recorder.list_activity_records(
        "add",
        ActivityScope(project_id="project", user_id="writer"),
        window_start=submitted_at - timedelta(minutes=1),
        window_end=submitted_at + timedelta(minutes=1),
        max_records=10,
        include_non_ok=False,
        feedback_processed=False,
    )
    processed = await recorder.list_activity_records(
        "add",
        ActivityScope(project_id="project", user_id="writer"),
        window_start=submitted_at - timedelta(minutes=1),
        window_end=submitted_at + timedelta(minutes=1),
        max_records=10,
        include_non_ok=False,
        feedback_processed=True,
    )
    assert pending == []
    assert [snapshot.record_id for snapshot in processed] == ["add-activity"]
