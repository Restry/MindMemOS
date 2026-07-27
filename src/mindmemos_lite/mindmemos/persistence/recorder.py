"""Business persistence for public add and search operation records.

The migrated vanilla pipeline deals in its normal request/result DTOs.  This
module is the only layer that translates those business values into the
backend-neutral ``Record`` primitives used by persistence v2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..infra.vector_store import (
    DatabaseScope,
    FilterExpression,
    FilterGroup,
    Page,
    Predicate,
    Record,
    RecordQuery,
    Sort,
    VectorDBService,
)
from ..logging import traced
from ..typing import (
    ActivityRecordSnapshot,
    ActivityScope,
    AddPipelineAsyncResult,
    AddPipelineInput,
    AddPipelineSyncResult,
    MemoryAddEventItem,
    MemoryRequestContext,
    SearchPipelineInput,
    SearchPipelineResult,
    SkillBinding,
)
from .v2 import ADD_RECORD_TABLE, SEARCH_RECORD_TABLE

_SCOPE_FIELDS = (
    "account_id",
    "project_id",
    "api_key_uuid",
    "user_id",
    "app_id",
    "session_id",
    "agent_id",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AddRecordView:
    """Business snapshot of one add request and its accumulated result."""

    add_record_id: str
    status: str
    mode: str
    messages: tuple[Mapping[str, Any], ...] = ()
    memories: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    request_submitted_at: datetime | None = None
    task_completed_at: datetime | None = None
    processing_at: datetime | None = None
    error: str | None = None
    request_id: str | None = None
    account_id: str | None = None
    project_id: str | None = None
    api_key_uuid: str | None = None
    user_id: str | None = None
    app_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    skill_bindings: tuple[Mapping[str, Any], ...] = ()
    score: float | None = None
    task_id: str | None = None
    feedback_processed: bool = False
    consolidation_status: str = "pending"
    consolidated_at: datetime | None = None
    consolidation_run_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRecordView:
    """Business snapshot preserving one query-to-result alignment."""

    search_record_id: str
    status: str
    query: str
    filters: Mapping[str, Any] | None
    top_k: int | None
    search_pipeline: str
    agentic: bool
    max_rounds: int
    rerank: bool
    score_threshold: float | None = None
    memories: tuple[Mapping[str, Any], ...] = ()
    request_submitted_at: datetime | None = None
    task_completed_at: datetime | None = None
    error: str | None = None
    request_id: str | None = None
    account_id: str | None = None
    project_id: str | None = None
    api_key_uuid: str | None = None
    user_id: str | None = None
    app_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None


class AddRecordPersistence:
    """Own the add-record lifecycle over ``add_record_v2``."""

    def __init__(self, service: VectorDBService) -> None:
        self._service = service

    @property
    def service(self) -> VectorDBService:
        return self._service

    @traced("persistence.add_record.record")
    async def record_add(
        self,
        inp: AddPipelineInput,
        result: AddPipelineSyncResult | AddPipelineAsyncResult | None,
        *,
        ctx: MemoryRequestContext,
        request_submitted_at: datetime,
        task_completed_at: datetime | None,
        add_record_id: str | None = None,
        skill_bindings: list[SkillBinding] | None = None,
        score: float | None = None,
        task_id: str | None = None,
        status: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> str:
        """Create or replace one add request record using business DTOs."""

        record_id = add_record_id or str(uuid4())
        payload: dict[str, Any] = {
            "schema_version": 2,
            "add_record_id": record_id,
            "request_id": ctx.request_id,
            "status": result.status if result is not None else (status or "processing"),
            "messages": _model_list_dump(inp.messages),
            "memories": (
                _model_list_dump(result.memories)
                if isinstance(result, AddPipelineSyncResult)
                else []
            ),
            "mode": inp.mode,
            "metadata": dict(inp.metadata),
            "event_timestamp_ms": inp.event_timestamp,
            "event_time": inp.event_timestamp_utc,
            "request_submitted_at": request_submitted_at,
            "task_completed_at": task_completed_at,
            "feedback_processed": False,
            "consolidation_status": "pending",
            "consolidated_at": None,
            "consolidation_run_id": None,
        }
        if skill_bindings is not None:
            payload["skill_bindings"] = _model_list_dump(skill_bindings, mode="json")
        if score is not None:
            payload["score"] = score
        if task_id is not None:
            payload["task_id"] = task_id
        if extra_payload:
            payload.update(extra_payload)

        await self._service.upsert_records(
            ADD_RECORD_TABLE,
            [
                Record(
                    table=ADD_RECORD_TABLE,
                    record_id=record_id,
                    scope=_write_scope(ctx),
                    payload=payload,
                )
            ],
        )
        return record_id

    async def record_add_input(
        self,
        inp: AddPipelineInput,
        *,
        ctx: MemoryRequestContext,
        request_submitted_at: datetime,
        add_record_id: str,
        status: str,
        skill_bindings: list[SkillBinding] | None = None,
        score: float | None = None,
        task_id: str | None = None,
    ) -> str:
        """Persist an add input before synchronous work or task submission."""

        return await self.record_add(
            inp,
            None,
            ctx=ctx,
            request_submitted_at=request_submitted_at,
            task_completed_at=None,
            add_record_id=add_record_id,
            skill_bindings=skill_bindings,
            score=score,
            task_id=task_id,
            status=status,
        )

    @traced("persistence.add_record.mark_processing")
    async def mark_add_processing(self, ctx: MemoryRequestContext, add_record_id: str) -> bool:
        return await self._patch(
            ctx,
            add_record_id,
            {"status": "processing", "processing_at": _utcnow()},
        )

    @traced("persistence.add_record.complete")
    async def mark_add_completed(
        self,
        ctx: MemoryRequestContext,
        add_record_id: str,
        result: AddPipelineSyncResult,
    ) -> bool:
        if not add_record_id:
            raise ValueError("Missing record id, cannot complete add record writeback")
        return await self._patch(
            ctx,
            add_record_id,
            {
                "status": result.status,
                "task_completed_at": _utcnow(),
                "memories": _model_list_dump(result.memories),
                "error": None,
            },
        )

    @traced("persistence.add_record.append_result")
    async def append_add_output(
        self,
        ctx: MemoryRequestContext,
        add_record_id: str,
        events: list[MemoryAddEventItem],
    ) -> bool:
        """Append episode output to one add record.

        As in the original schema-add path, callers must serialize concurrent
        appends for the same add record.
        """

        record = await self._find(ctx, add_record_id)
        if record is None:
            return False
        memories = list(record.payload.get("memories") or [])
        memories.extend(_model_list_dump(events))
        await self._service.patch_record(
            ADD_RECORD_TABLE,
            record.scope,
            add_record_id,
            {
                "status": "ok",
                "task_completed_at": _utcnow(),
                "memories": memories,
                "error": None,
            },
        )
        return True

    @traced("persistence.add_record.fail")
    async def mark_add_failed(
        self,
        ctx: MemoryRequestContext,
        add_record_id: str,
        error: str,
    ) -> bool:
        return await self._patch(
            ctx,
            add_record_id,
            {
                "status": "error",
                "error": error,
                "task_completed_at": _utcnow(),
            },
        )

    async def patch(
        self,
        ctx: MemoryRequestContext,
        add_record_id: str,
        changes: Mapping[str, Any],
    ) -> bool:
        """Patch dreaming/feedback bookkeeping without exposing record primitives."""

        return await self._patch(ctx, add_record_id, changes)

    async def get(self, ctx: MemoryRequestContext, add_record_id: str) -> AddRecordView | None:
        record = await self._find(ctx, add_record_id)
        return _add_record_view(record) if record is not None else None

    async def get_many(
        self,
        ctx: MemoryRequestContext,
        add_record_ids: Sequence[str],
    ) -> list[AddRecordView]:
        records = await _records_for_ids(
            self._service,
            ADD_RECORD_TABLE,
            "add_record_id",
            ctx,
            add_record_ids,
        )
        by_id = {record.record_id: record for record in records}
        return [
            _add_record_view(by_id[record_id])
            for record_id in _dedupe(add_record_ids)
            if record_id in by_id
        ]

    async def list(
        self,
        ctx: MemoryRequestContext,
        *,
        statuses: Sequence[str] = (),
        modes: Sequence[str] = (),
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        task_id: str | None = None,
        feedback_processed: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[AddRecordView], str | None]:
        filters = _and_filters(
            Predicate(field="status", op="in", value=tuple(statuses)) if statuses else None,
            Predicate(field="mode", op="in", value=tuple(modes)) if modes else None,
            Predicate(field="request_submitted_at", op="gte", value=submitted_from)
            if submitted_from is not None
            else None,
            Predicate(field="request_submitted_at", op="lte", value=submitted_to)
            if submitted_to is not None
            else None,
            Predicate(field="task_id", op="eq", value=task_id) if task_id is not None else None,
            Predicate(field="feedback_processed", op="eq", value=feedback_processed)
            if feedback_processed is not None
            else None,
        )
        records, next_cursor = await self._service.query_records(
            ADD_RECORD_TABLE,
            RecordQuery(
                scope=_query_scope(ctx),
                filters=filters,
                sort=(Sort(field="request_submitted_at", direction="desc"),),
                page=Page(limit=max(1, limit), cursor=cursor),
            ),
        )
        return [_add_record_view(record) for record in records], next_cursor

    async def _find(self, ctx: MemoryRequestContext, add_record_id: str) -> Record | None:
        return await _find_record(
            self._service,
            ADD_RECORD_TABLE,
            "add_record_id",
            ctx,
            add_record_id,
        )

    async def _patch(
        self,
        ctx: MemoryRequestContext,
        add_record_id: str,
        changes: Mapping[str, Any],
    ) -> bool:
        record = await self._find(ctx, add_record_id)
        if record is None:
            return False
        await self._service.patch_record(
            ADD_RECORD_TABLE,
            record.scope,
            add_record_id,
            changes,
        )
        return True


class SearchRecordPersistence:
    """Own query/result audit snapshots over ``search_record_v2``."""

    def __init__(self, service: VectorDBService) -> None:
        self._service = service

    @traced("persistence.search_record.record")
    async def record_search(
        self,
        inp: SearchPipelineInput,
        result: SearchPipelineResult | None,
        *,
        ctx: MemoryRequestContext,
        request_submitted_at: datetime,
        task_completed_at: datetime,
        search_record_id: str | None = None,
        error: str | None = None,
    ) -> str:
        """Persist one search request and the exact result snapshot returned."""

        record_id = search_record_id or str(uuid4())
        payload = {
            "schema_version": 2,
            "search_record_id": record_id,
            "request_id": ctx.request_id,
            "status": result.status if result is not None else "error",
            "query": inp.query,
            "filters": dict(inp.filters) if inp.filters else None,
            "top_k": inp.top_k,
            "search_pipeline": inp.search_pipeline,
            "agentic": inp.agentic,
            "max_rounds": inp.max_rounds,
            "rerank": inp.rerank,
            "score_threshold": inp.score_threshold,
            "memories": _model_list_dump(result.memories) if result is not None else [],
            "request_submitted_at": request_submitted_at,
            "task_completed_at": task_completed_at,
            "error": error,
        }
        await self._service.upsert_records(
            SEARCH_RECORD_TABLE,
            [
                Record(
                    table=SEARCH_RECORD_TABLE,
                    record_id=record_id,
                    scope=_write_scope(ctx),
                    payload=payload,
                )
            ],
        )
        return record_id

    async def get(self, ctx: MemoryRequestContext, search_record_id: str) -> SearchRecordView | None:
        record = await self._find(ctx, search_record_id)
        return _search_record_view(record) if record is not None else None

    async def get_many(
        self,
        ctx: MemoryRequestContext,
        search_record_ids: Sequence[str],
    ) -> list[SearchRecordView]:
        records = await _records_for_ids(
            self._service,
            SEARCH_RECORD_TABLE,
            "search_record_id",
            ctx,
            search_record_ids,
        )
        by_id = {record.record_id: record for record in records}
        return [
            _search_record_view(by_id[record_id])
            for record_id in _dedupe(search_record_ids)
            if record_id in by_id
        ]

    async def list(
        self,
        ctx: MemoryRequestContext,
        *,
        statuses: Sequence[str] = (),
        search_pipelines: Sequence[str] = (),
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        agentic: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[SearchRecordView], str | None]:
        filters = _and_filters(
            Predicate(field="status", op="in", value=tuple(statuses)) if statuses else None,
            Predicate(field="search_pipeline", op="in", value=tuple(search_pipelines))
            if search_pipelines
            else None,
            Predicate(field="request_submitted_at", op="gte", value=submitted_from)
            if submitted_from is not None
            else None,
            Predicate(field="request_submitted_at", op="lte", value=submitted_to)
            if submitted_to is not None
            else None,
            Predicate(field="agentic", op="eq", value=agentic) if agentic is not None else None,
        )
        records, next_cursor = await self._service.query_records(
            SEARCH_RECORD_TABLE,
            RecordQuery(
                scope=_query_scope(ctx),
                filters=filters,
                sort=(Sort(field="request_submitted_at", direction="desc"),),
                page=Page(limit=max(1, limit), cursor=cursor),
            ),
        )
        return [_search_record_view(record) for record in records], next_cursor

    async def _find(self, ctx: MemoryRequestContext, search_record_id: str) -> Record | None:
        return await _find_record(
            self._service,
            SEARCH_RECORD_TABLE,
            "search_record_id",
            ctx,
            search_record_id,
        )


class MemoryOperationRecorder:
    """Compatibility facade used by the migrated add/search orchestration."""

    def __init__(
        self,
        add_records: AddRecordPersistence,
        search_records: SearchRecordPersistence,
    ) -> None:
        self.add_records = add_records
        self.search_records = search_records

    @classmethod
    def from_service(cls, service: VectorDBService) -> "MemoryOperationRecorder":
        return cls(
            AddRecordPersistence(service),
            SearchRecordPersistence(service),
        )

    async def record_add(self, *args: Any, **kwargs: Any) -> str:
        return await self.add_records.record_add(*args, **kwargs)

    async def record_add_input(self, *args: Any, **kwargs: Any) -> str:
        return await self.add_records.record_add_input(*args, **kwargs)

    async def mark_add_processing(self, *args: Any, **kwargs: Any) -> bool:
        return await self.add_records.mark_add_processing(*args, **kwargs)

    async def mark_add_completed(self, *args: Any, **kwargs: Any) -> bool:
        return await self.add_records.mark_add_completed(*args, **kwargs)

    async def append_add_output(self, *args: Any, **kwargs: Any) -> bool:
        return await self.add_records.append_add_output(*args, **kwargs)

    async def mark_add_failed(self, *args: Any, **kwargs: Any) -> bool:
        return await self.add_records.mark_add_failed(*args, **kwargs)

    async def record_search(self, *args: Any, **kwargs: Any) -> str:
        return await self.search_records.record_search(*args, **kwargs)

    async def patch_add_record(self, *args: Any, **kwargs: Any) -> bool:
        return await self.add_records.patch(*args, **kwargs)

    async def list_activity_records(
        self,
        kind: str,
        scope: ActivityScope,
        *,
        window_start: datetime,
        window_end: datetime,
        max_records: int | None,
        include_non_ok: bool,
        feedback_processed: bool | None = None,
    ) -> list[ActivityRecordSnapshot]:
        """Read add/search audit records through one backend-neutral activity port."""

        if kind == "add":
            table = ADD_RECORD_TABLE
            record_id_key = "add_record_id"
        elif kind == "search":
            table = SEARCH_RECORD_TABLE
            record_id_key = "search_record_id"
        else:
            raise ValueError(f"unsupported activity record kind: {kind!r}")

        query_scope = DatabaseScope(
            {
                field_name: getattr(scope, field_name)
                for field_name in _SCOPE_FIELDS
                if getattr(scope, field_name) is not None
            }
        )
        filters = _and_filters(
            Predicate(field="request_submitted_at", op="gte", value=window_start),
            Predicate(field="request_submitted_at", op="lte", value=window_end),
            None if include_non_ok else Predicate(field="status", op="eq", value="ok"),
            (
                Predicate(field="feedback_processed", op="eq", value=feedback_processed)
                if kind == "add" and feedback_processed is not None
                else None
            ),
        )
        records: list[Record] = []
        cursor: str | None = None
        while max_records is None or len(records) < max_records:
            limit = 2000 if max_records is None else min(2000, max_records - len(records))
            page, cursor = await self.add_records.service.query_records(
                table,
                RecordQuery(
                    scope=query_scope,
                    filters=filters,
                    sort=(Sort(field="request_submitted_at", direction="desc"),),
                    page=Page(limit=max(1, limit), cursor=cursor),
                ),
            )
            records.extend(page)
            if cursor is None:
                break

        snapshots: list[ActivityRecordSnapshot] = []
        for record in records:
            payload = {**dict(record.scope.items()), **dict(record.payload)}
            snapshots.append(
                ActivityRecordSnapshot(
                    record_id=str(payload.get(record_id_key) or record.record_id),
                    payload=payload,
                )
            )
        return snapshots


class SchemaAddBufferPersistence:
    """Reserved for the schema-add migration, which is outside this change."""


async def _find_record(
    service: VectorDBService,
    table: str,
    primary_key: str,
    ctx: MemoryRequestContext,
    record_id: str,
) -> Record | None:
    records, _ = await service.query_records(
        table,
        RecordQuery(
            scope=_query_scope(ctx),
            filters=Predicate(field=primary_key, op="eq", value=record_id),
            page=Page(limit=2),
        ),
    )
    if len(records) > 1:
        raise RuntimeError(f"{table} record {record_id!r} is ambiguous in the supplied scope")
    return records[0] if records else None


async def _records_for_ids(
    service: VectorDBService,
    table: str,
    primary_key: str,
    ctx: MemoryRequestContext,
    record_ids: Sequence[str],
) -> list[Record]:
    ids = _dedupe(record_ids)
    if not ids:
        return []
    records, _ = await service.query_records(
        table,
        RecordQuery(
            scope=_query_scope(ctx),
            filters=Predicate(field=primary_key, op="in", value=tuple(ids)),
            page=Page(limit=len(ids)),
        ),
    )
    return records


def _write_scope(ctx: MemoryRequestContext) -> DatabaseScope:
    return DatabaseScope({field_name: getattr(ctx, field_name, None) for field_name in _SCOPE_FIELDS})


def _query_scope(ctx: MemoryRequestContext) -> DatabaseScope:
    return DatabaseScope(project_id=ctx.project_id)


def _model_list_dump(values: Sequence[Any], *, mode: str = "python") -> list[dict[str, Any]]:
    return [
        value.model_dump(mode=mode) if hasattr(value, "model_dump") else dict(value)
        for value in values
    ]


def _and_filters(*filters: FilterExpression | None) -> FilterExpression | None:
    clauses = tuple(value for value in filters if value is not None)
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return FilterGroup(operator="and", clauses=clauses)


def _add_record_view(record: Record) -> AddRecordView:
    payload = record.payload
    scope = record.scope
    return AddRecordView(
        add_record_id=str(payload.get("add_record_id") or record.record_id),
        status=str(payload.get("status") or ""),
        mode=str(payload.get("mode") or ""),
        messages=tuple(dict(value) for value in payload.get("messages") or []),
        memories=tuple(dict(value) for value in payload.get("memories") or []),
        metadata=dict(payload.get("metadata") or {}),
        request_submitted_at=_datetime(payload.get("request_submitted_at")),
        task_completed_at=_datetime(payload.get("task_completed_at")),
        processing_at=_datetime(payload.get("processing_at")),
        error=_optional_text(payload.get("error")),
        request_id=_optional_text(payload.get("request_id")),
        skill_bindings=tuple(dict(value) for value in payload.get("skill_bindings") or []),
        score=_optional_float(payload.get("score")),
        task_id=_optional_text(payload.get("task_id")),
        feedback_processed=bool(payload.get("feedback_processed", False)),
        consolidation_status=str(payload.get("consolidation_status") or "pending"),
        consolidated_at=_datetime(payload.get("consolidated_at")),
        consolidation_run_id=_optional_text(payload.get("consolidation_run_id")),
        **_scope_kwargs(scope),
    )


def _search_record_view(record: Record) -> SearchRecordView:
    payload = record.payload
    scope = record.scope
    top_k = payload.get("top_k")
    return SearchRecordView(
        search_record_id=str(payload.get("search_record_id") or record.record_id),
        status=str(payload.get("status") or ""),
        query=str(payload.get("query") or ""),
        filters=dict(payload["filters"]) if payload.get("filters") else None,
        top_k=int(top_k) if top_k is not None else None,
        search_pipeline=str(payload.get("search_pipeline") or ""),
        agentic=bool(payload.get("agentic", False)),
        max_rounds=int(payload.get("max_rounds") or 0),
        rerank=bool(payload.get("rerank", False)),
        score_threshold=_optional_float(payload.get("score_threshold")),
        memories=tuple(dict(value) for value in payload.get("memories") or []),
        request_submitted_at=_datetime(payload.get("request_submitted_at")),
        task_completed_at=_datetime(payload.get("task_completed_at")),
        error=_optional_text(payload.get("error")),
        request_id=_optional_text(payload.get("request_id")),
        **_scope_kwargs(scope),
    )


def _scope_kwargs(scope: DatabaseScope) -> dict[str, str | None]:
    return {
        field_name: _optional_text(scope.get(field_name))
        for field_name in _SCOPE_FIELDS
    }


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AddRecordPersistence",
    "AddRecordView",
    "MemoryOperationRecorder",
    "SchemaAddBufferPersistence",
    "SearchRecordPersistence",
    "SearchRecordView",
]
