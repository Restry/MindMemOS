"""Read-only collector for recent add / search activity logs.

The ``RecentActivityCollector`` reads Lite's add/search recorder port inside
one time window and reshapes the records
into a :class:`RecentActivityBundle`. It is a stateless atomic component: it only
reads activity snapshots, never writes, never calls the LLM, and never
hydrates the memory table. All dreaming / feedback specific judgment is left to
the downstream pipelines.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ...errors import ActivityCollectionError
from ...logging import get_logger
from ...typing import (
    ActivityMessage,
    ActivityRecordContext,
    ActivityRecordSnapshot,
    ActivityScope,
    AddActivityEvent,
    ConversationActivity,
    DialogueMessage,
    FileMessage,
    MemoryRef,
    RecentActivityBundle,
    SearchActivityEvent,
    TextMessage,
    UrlMessage,
    WrittenMemoryRef,
)

logger = get_logger(__name__)

DEFAULT_LOOKBACK = timedelta(hours=72)
DEFAULT_MAX_RECORDS = 2000


class ActivityRecordStore(Protocol):
    """Read recent backend-neutral audit snapshots for one activity kind."""

    async def list_activity_records(
        self,
        kind: str,
        scope: ActivityScope,
        *,
        window_start: datetime,
        window_end: datetime,
        max_records: int | None,
        include_non_ok: bool,
    ) -> list[ActivityRecordSnapshot]: ...


class RecentActivityCollector:
    """Collect a recent-activity bundle from the add / search audit logs."""

    def __init__(self, store: ActivityRecordStore) -> None:
        self._store = store

    async def collect(
        self,
        scope: ActivityScope,
        *,
        lookback: timedelta = DEFAULT_LOOKBACK,
        window_end: datetime | None = None,
        max_records: int | None = DEFAULT_MAX_RECORDS,
        include_non_ok: bool = False,
        add_must: list[Any] | None = None,
        add_should: list[Any] | None = None,
        add_must_not: list[Any] | None = None,
        search_must: list[Any] | None = None,
        search_should: list[Any] | None = None,
        search_must_not: list[Any] | None = None,
    ) -> RecentActivityBundle:
        """Handle collect."""

        window_end = window_end or datetime.now(UTC)
        window_start = window_end - lookback
        if any((add_must, add_should, add_must_not, search_must, search_should, search_must_not)):
            raise ActivityCollectionError("Lite activity collection does not accept backend-specific filters")

        try:
            add_records = await self._store.list_activity_records(
                "add",
                scope,
                window_start=window_start,
                window_end=window_end,
                max_records=max_records,
                include_non_ok=include_non_ok,
            )
            search_records = await self._store.list_activity_records(
                "search",
                scope,
                window_start=window_start,
                window_end=window_end,
                max_records=max_records,
                include_non_ok=include_non_ok,
            )
        except ActivityCollectionError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize persistence failures
            logger.warning("activity read failed", error=str(exc))
            raise ActivityCollectionError("failed to read recent activity records") from exc

        groups = self._group_by_session(add_records, search_records)
        conversations = [self._build_conversation(group) for group in groups]

        return RecentActivityBundle(
            window_start=window_start,
            window_end=window_end,
            scope=scope,
            conversations=conversations,
            search_events=[event for conv in conversations for event in conv.search_events],
            recalled_memories=self._dedup_recalled(conversations),
            written_memories=self._dedup_written(conversations),
        )

    def _group_by_session(
        self,
        add_records: list[ActivityRecordSnapshot],
        search_records: list[ActivityRecordSnapshot],
    ) -> list[_SessionGroup]:
        """Group add / search records into per-session buckets.

        Records carrying a ``session_id`` share one group; records without one
        degrade to "one record == one conversation" via a per-record sentinel
        key. Insertion order follows the scroll order (newest first).
        """

        groups: OrderedDict[str, _SessionGroup] = OrderedDict()

        def bucket(record_id: str, session_id: str | None) -> _SessionGroup:
            key = session_id if session_id else f"__record__:{record_id}"
            group = groups.get(key)
            if group is None:
                group = _SessionGroup(session_id=session_id)
                groups[key] = group
            return group

        for record in add_records:
            record_id = _record_id(record, "add_record_id")
            session_id = record.payload.get("session_id")
            bucket(record_id, session_id).add_records.append((record_id, record))
        for record in search_records:
            record_id = _record_id(record, "search_record_id")
            session_id = record.payload.get("session_id")
            bucket(record_id, session_id).search_records.append((record_id, record))

        return list(groups.values())

    def _build_conversation(self, group: _SessionGroup) -> ConversationActivity:
        add_record_ids: list[str] = []
        add_events: list[AddActivityEvent] = []
        messages: list[ActivityMessage] = []
        written_index: OrderedDict[str, WrittenMemoryRef] = OrderedDict()
        timestamps: list[datetime] = []
        user_id = agent_id = app_id = None

        for record_id, record in sorted(group.add_records, key=_add_record_order_key):
            payload = record.payload
            add_record_ids.append(record_id)
            user_id = user_id or payload.get("user_id")
            agent_id = agent_id or payload.get("agent_id")
            app_id = app_id or payload.get("app_id")
            submitted_at = _parse_dt(payload.get("request_submitted_at"))
            completed_at = _parse_dt(payload.get("task_completed_at"))
            timestamps.extend(ts for ts in (submitted_at, completed_at) if ts is not None)
            record_messages = _parse_messages(payload.get("messages"))
            messages.extend(record_messages)
            add_events.append(
                AddActivityEvent(
                    add_record_id=record_id,
                    context=_record_context(payload),
                    occurred_at=submitted_at,
                    completed_at=completed_at,
                    status=payload.get("status"),
                    messages=record_messages,
                    memory_payloads=[dict(item) for item in payload.get("memories") or [] if isinstance(item, dict)],
                )
            )
            self._merge_written(written_index, payload.get("memories"), record_id, payload, submitted_at)

        search_events: list[SearchActivityEvent] = []
        search_record_ids: list[str] = []
        for record_id, record in group.search_records:
            payload = record.payload
            search_record_ids.append(record_id)
            user_id = user_id or payload.get("user_id")
            agent_id = agent_id or payload.get("agent_id")
            app_id = app_id or payload.get("app_id")
            occurred_at = _parse_dt(payload.get("request_submitted_at"))
            completed_at = _parse_dt(payload.get("task_completed_at"))
            timestamps.extend(ts for ts in (occurred_at, completed_at) if ts is not None)
            search_events.append(self._build_search_event(record_id, payload, occurred_at))

        written_memories = list(written_index.values())
        return ConversationActivity(
            session_id=group.session_id,
            user_id=user_id,
            agent_id=agent_id,
            app_id=app_id,
            occurred_at=min(timestamps) if timestamps else None,
            last_occurred_at=max(timestamps) if timestamps else None,
            add_record_ids=add_record_ids,
            search_record_ids=search_record_ids,
            add_events=add_events,
            messages=messages,
            search_events=search_events,
            written_memories=written_memories,
            written_memory_ids=[ref.memory_id for ref in written_memories],
            # Collector does not apply feedback / dreaming filtering (e.g. on
            # feedback_processed or consolidation_status); it exposes every add
            # record as a candidate and lets downstream pipelines filter.
            feedback_add_record_ids=list(add_record_ids),
            dreaming_add_record_ids=list(add_record_ids),
        )

    def _build_search_event(
        self,
        search_record_id: str,
        payload: dict[str, Any],
        occurred_at: datetime | None,
    ) -> SearchActivityEvent:
        recalled: list[MemoryRef] = []
        for rank, item in enumerate(payload.get("memories") or []):
            if not isinstance(item, dict):
                continue
            memory_id = item.get("id")
            if memory_id is None:
                continue
            recalled.append(
                MemoryRef(
                    memory_id=str(memory_id),
                    content=item.get("memory"),
                    search_record_id=search_record_id,
                    rank=rank,
                    score=item.get("score"),
                    payload=dict(item),
                )
            )
        return SearchActivityEvent(
            search_record_id=search_record_id,
            context=_record_context(payload),
            occurred_at=occurred_at,
            query=payload.get("query") or "",
            filters=payload.get("filters"),
            top_k=payload.get("top_k"),
            deep_search=payload.get("deep_search"),
            status=payload.get("status"),
            recalled_memories=recalled,
        )

    def _merge_written(
        self,
        index: OrderedDict[str, WrittenMemoryRef],
        memories: Any,
        record_id: str,
        payload: dict[str, Any],
        seen_at: datetime | None,
    ) -> None:
        session_id = payload.get("session_id")
        user_id = payload.get("user_id")
        for item in memories or []:
            if not isinstance(item, dict):
                continue
            memory_id = item.get("memory_id")
            if memory_id is None:
                continue
            memory_id = str(memory_id)
            ref = index.get(memory_id)
            if ref is None:
                index[memory_id] = WrittenMemoryRef(
                    memory_id=memory_id,
                    content=item.get("content"),
                    operation=item.get("operation"),
                    add_record_ids=[record_id],
                    session_id=session_id,
                    user_id=user_id,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    payloads=[dict(item)],
                )
                continue
            if record_id not in ref.add_record_ids:
                ref.add_record_ids.append(record_id)
            ref.payloads.append(dict(item))
            # Keep the most recent operation / content for the merged ref.
            if _is_newer(seen_at, ref.last_seen_at):
                ref.last_seen_at = seen_at
                ref.operation = item.get("operation") or ref.operation
                if item.get("content") is not None:
                    ref.content = item.get("content")
            if _is_older(seen_at, ref.first_seen_at):
                ref.first_seen_at = seen_at

    # Global de-duplication

    def _dedup_recalled(self, conversations: list[ConversationActivity]) -> list[MemoryRef]:
        seen: OrderedDict[str, MemoryRef] = OrderedDict()
        for conv in conversations:
            for event in conv.search_events:
                for ref in event.recalled_memories:
                    seen.setdefault(ref.memory_id, ref)
        return list(seen.values())

    def _dedup_written(self, conversations: list[ConversationActivity]) -> list[WrittenMemoryRef]:
        merged: OrderedDict[str, WrittenMemoryRef] = OrderedDict()
        for conv in conversations:
            for ref in conv.written_memories:
                existing = merged.get(ref.memory_id)
                if existing is None:
                    merged[ref.memory_id] = ref.model_copy(deep=True)
                    continue
                for rid in ref.add_record_ids:
                    if rid not in existing.add_record_ids:
                        existing.add_record_ids.append(rid)
                existing.payloads.extend(ref.payloads)
                if _is_newer(ref.last_seen_at, existing.last_seen_at):
                    existing.last_seen_at = ref.last_seen_at
                    existing.operation = ref.operation or existing.operation
                    if ref.content is not None:
                        existing.content = ref.content
                if _is_older(ref.first_seen_at, existing.first_seen_at):
                    existing.first_seen_at = ref.first_seen_at
        return list(merged.values())


class _SessionGroup:
    """Mutable per-session accumulator of add / search records."""

    __slots__ = ("session_id", "add_records", "search_records")

    def __init__(self, *, session_id: str | None) -> None:
        self.session_id = session_id
        self.add_records: list[tuple[str, ActivityRecordSnapshot]] = []
        self.search_records: list[tuple[str, ActivityRecordSnapshot]] = []


def _record_id(record: ActivityRecordSnapshot, payload_key: str) -> str:
    """Prefer an explicit id in the payload, fall back to the snapshot id."""

    value = record.payload.get(payload_key)
    return str(value) if value is not None else record.record_id


def _add_record_order_key(item: tuple[str, ActivityRecordSnapshot]) -> tuple[str, int]:
    _, record = item
    payload = record.payload
    sequence = payload.get("buffer_sequence")
    if sequence is not None:
        return ("", int(sequence))
    submitted_at = _parse_dt(payload.get("request_submitted_at"))
    return (submitted_at.isoformat() if submitted_at is not None else "", 0)


def _record_context(payload: dict[str, Any]) -> ActivityRecordContext:
    return ActivityRecordContext(
        project_id=payload.get("project_id") or "",
        request_id=payload.get("request_id"),
        account_id=payload.get("account_id"),
        api_key_uuid=payload.get("api_key_uuid"),
        user_id=payload.get("user_id"),
        session_id=payload.get("session_id"),
        agent_id=payload.get("agent_id"),
        app_id=payload.get("app_id"),
    )


def _parse_messages(messages: Any) -> list[ActivityMessage]:
    parsed: list[ActivityMessage] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        message = _parse_message(item)
        if message is not None:
            parsed.append(message)
    return parsed


def _parse_message(item: dict[str, Any]) -> ActivityMessage | None:
    """Reconstruct one stored message dict into its original DTO by key shape."""

    try:
        if "url" in item:
            return UrlMessage(**item)
        if "file_path" in item or "file_name" in item:
            return FileMessage(**item)
        if "role" in item and "content" in item:
            return DialogueMessage(**item)
        if "text" in item:
            return TextMessage(**item)
    except Exception as exc:  # noqa: BLE001 - tolerate malformed audit payloads
        logger.warning("skip unparseable activity message", error=str(exc))
        return None
    return None


def _parse_dt(value: Any) -> datetime | None:
    """Parse a stored timestamp into a timezone-aware datetime, else None."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _is_newer(candidate: datetime | None, current: datetime | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate > current


def _is_older(candidate: datetime | None, current: datetime | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate < current
