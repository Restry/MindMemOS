"""Aggregate repository for immutable Skill versions and mutable family state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..errors import SkillConflictError, SkillNotFoundError
from ..infra.database import (
    DatabaseScope,
    DatabaseUnitOfWork,
    FilterGroup,
    Page,
    Predicate,
    RecordQuery,
    ScopedDatabase,
    Sort,
)
from ..persistence import (
    SKILL_FAMILY_STATE_TABLE,
    SKILL_TABLE,
    SkillFamilyStateRecord,
    SkillRecord,
    from_database_record,
    to_database_record,
)
from .bundle import parse_version_label


class SkillRepository:
    """Enforce Skill-family invariants over the generic database boundary."""

    def __init__(self, database: ScopedDatabase) -> None:
        self._database = database

    @property
    def database(self) -> ScopedDatabase:
        return self._database

    async def create_version(
        self,
        record: SkillRecord,
        *,
        now: datetime,
        make_effective: bool = False,
        expected_effective_version_id: str | None = None,
        pending_operation: Mapping[str, Any] | None = None,
    ) -> SkillFamilyStateRecord:
        """Create one immutable version and update its family atomically."""

        async with self._database.transaction() as transaction:
            if await self._get_version(transaction, record.version_id) is not None:
                raise SkillConflictError(f"version already exists: {record.version_id}")

            existing_versions = await self._query_versions(transaction, skill_id=record.skill_id)
            state = await self._get_state(transaction, record.skill_id)
            await self._validate_alias(transaction, record)
            if not existing_versions:
                if state is not None:
                    raise SkillConflictError(f"Skill family state exists without versions: {record.skill_id}")
                if record.parent_version_ids:
                    raise SkillConflictError("a root Skill version cannot declare parents")
                state = SkillFamilyStateRecord(
                    skill_id=record.skill_id,
                    effective_version_id=record.version_id,
                    pending_operations=[dict(pending_operation)] if pending_operation is not None else [],
                    created_at=now,
                    updated_at=now,
                )
            else:
                if state is None:
                    raise SkillConflictError(f"Skill family has versions but no state: {record.skill_id}")
                if not record.parent_version_ids:
                    raise SkillConflictError("a non-root Skill version requires at least one parent")
                await self._validate_parents(transaction, record)
                self._validate_family_identity(record, existing_versions)
                self._validate_version_label(record, existing_versions)
                if (
                    expected_effective_version_id is not None
                    and state.effective_version_id != expected_effective_version_id
                ):
                    raise SkillConflictError(
                        "effective version changed concurrently: "
                        f"expected {expected_effective_version_id}, got {state.effective_version_id}"
                    )
                state_payload = state.model_dump(mode="python")
                state_payload["updated_at"] = now
                if make_effective:
                    state_payload["effective_version_id"] = record.version_id
                if pending_operation is not None:
                    state_payload["pending_operations"] = [
                        *state.pending_operations,
                        dict(pending_operation),
                    ]
                state = SkillFamilyStateRecord.model_validate(state_payload)

            await transaction.upsert_records(SKILL_TABLE, [to_database_record(record)])
            await transaction.upsert_records(SKILL_FAMILY_STATE_TABLE, [to_database_record(state)])
            return state

    async def get_version(self, version_id: str) -> SkillRecord:
        record = await self._get_version(self._database, version_id)
        if record is None:
            raise SkillNotFoundError(f"Skill version not found: {version_id}")
        return record

    async def get_family_state(self, skill_id: str) -> SkillFamilyStateRecord:
        state = await self._get_state(self._database, skill_id)
        if state is None:
            raise SkillNotFoundError(f"Skill family not found: {skill_id}")
        return state

    async def resolve_skill_id(self, skill_ref: str) -> str:
        direct = await self._get_state(self._database, skill_ref)
        if direct is not None:
            return direct.skill_id
        matches = await self.query_versions(alias=skill_ref)
        skill_ids = sorted({record.skill_id for record in matches})
        if not skill_ids:
            raise SkillNotFoundError(f"Skill family not found: {skill_ref}")
        if len(skill_ids) > 1:
            raise SkillConflictError(f"Skill alias is ambiguous: {skill_ref}")
        return skill_ids[0]

    async def list_family_states(self) -> list[SkillFamilyStateRecord]:
        records = await _query_all(
            self._database,
            SKILL_FAMILY_STATE_TABLE,
            sort=(Sort(field="created_at"), Sort(field="skill_id")),
        )
        return [from_database_record(record, SkillFamilyStateRecord) for record in records]

    async def list_versions(self, skill_ref: str) -> list[SkillRecord]:
        skill_id = await self.resolve_skill_id(skill_ref)
        return await self.query_versions(skill_id=skill_id)

    async def query_versions(
        self,
        *,
        skill_id: str | None = None,
        alias: str | None = None,
        version_id: str | None = None,
        content_hash: str | None = None,
    ) -> list[SkillRecord]:
        return await self._query_versions(
            self._database,
            skill_id=skill_id,
            alias=alias,
            version_id=version_id,
            content_hash=content_hash,
        )

    async def find_snapshot_matches(self, local_snapshot_hash: str) -> list[SkillRecord]:
        records = await self.query_versions()
        return [
            record
            for record in records
            if isinstance(record.metadata.get("snapshot"), dict)
            and record.metadata["snapshot"].get("local_snapshot_hash") == local_snapshot_hash
        ]

    async def compare_and_set_effective_version(
        self,
        skill_ref: str,
        *,
        version_id: str,
        expected_version_id: str,
        updated_at: datetime,
    ) -> bool:
        """Move only the effective pointer when the caller's old value still matches."""

        skill_id = await self.resolve_skill_id(skill_ref)
        async with self._database.transaction() as transaction:
            target = await self._get_version(transaction, version_id)
            if target is None or target.skill_id != skill_id:
                raise SkillConflictError(f"version {version_id} does not belong to Skill {skill_id}")
            return await transaction.compare_and_swap_record(
                SKILL_FAMILY_STATE_TABLE,
                DatabaseScope(),
                skill_id,
                expected={"effective_version_id": expected_version_id},
                changes={
                    "effective_version_id": version_id,
                    "updated_at": updated_at,
                },
            )

    async def set_effective_version(
        self,
        skill_ref: str,
        *,
        version_id: str,
        expected_version_id: str | None,
        updated_at: datetime,
    ) -> SkillFamilyStateRecord:
        skill_id = await self.resolve_skill_id(skill_ref)
        state = await self.get_family_state(skill_id)
        expected = expected_version_id or state.effective_version_id
        changed = await self.compare_and_set_effective_version(
            skill_id,
            version_id=version_id,
            expected_version_id=expected,
            updated_at=updated_at,
        )
        if not changed:
            current = await self.get_family_state(skill_id)
            raise SkillConflictError(
                f"effective version changed concurrently: expected {expected}, got {current.effective_version_id}"
            )
        return await self.get_family_state(skill_id)

    async def _validate_alias(self, transaction: DatabaseUnitOfWork, record: SkillRecord) -> None:
        identity_matches = await self._query_versions(transaction, alias=record.skill_id)
        if any(match.skill_id != record.skill_id for match in identity_matches):
            raise SkillConflictError(f"Skill id conflicts with an existing alias: {record.skill_id}")
        if record.alias is None:
            return
        matches = await self._query_versions(transaction, alias=record.alias)
        if any(match.skill_id != record.skill_id for match in matches):
            raise SkillConflictError(f"Skill alias already exists: {record.alias}")
        if await self._get_state(transaction, record.alias) is not None and record.alias != record.skill_id:
            raise SkillConflictError(f"Skill alias conflicts with a Skill id: {record.alias}")

    async def _validate_parents(self, transaction: DatabaseUnitOfWork, record: SkillRecord) -> None:
        parents = [await self._get_version(transaction, version_id) for version_id in record.parent_version_ids]
        missing = [
            version_id for version_id, parent in zip(record.parent_version_ids, parents, strict=True) if parent is None
        ]
        if missing:
            raise SkillConflictError(f"parent Skill versions do not exist: {', '.join(missing)}")
        if any(parent is not None and parent.skill_id != record.skill_id for parent in parents):
            raise SkillConflictError("all parent versions must belong to the same Skill family")

    @staticmethod
    def _validate_family_identity(record: SkillRecord, existing: list[SkillRecord]) -> None:
        family = existing[0]
        if record.alias != family.alias:
            raise SkillConflictError("a Skill family alias is immutable")

    @staticmethod
    def _validate_version_label(record: SkillRecord, existing: list[SkillRecord]) -> None:
        incoming = parse_version_label(record.version_label)
        labels = {item.version_label for item in existing}
        if record.version_label in labels:
            raise SkillConflictError(f"version label already exists in Skill family: {record.version_label}")
        current = max(parse_version_label(label) for label in labels)
        if incoming <= current:
            raise SkillConflictError(
                f"version label must increase monotonically: {record.version_label} <= {'.'.join(map(str, current))}"
            )

    @staticmethod
    async def _get_version(database: DatabaseUnitOfWork, version_id: str) -> SkillRecord | None:
        records = await database.get_records(SKILL_TABLE, DatabaseScope(), [version_id])
        return from_database_record(records[0], SkillRecord) if records else None

    @staticmethod
    async def _get_state(database: DatabaseUnitOfWork, skill_id: str) -> SkillFamilyStateRecord | None:
        records = await database.get_records(SKILL_FAMILY_STATE_TABLE, DatabaseScope(), [skill_id])
        return from_database_record(records[0], SkillFamilyStateRecord) if records else None

    @staticmethod
    async def _query_versions(
        database: DatabaseUnitOfWork,
        *,
        skill_id: str | None = None,
        alias: str | None = None,
        version_id: str | None = None,
        content_hash: str | None = None,
    ) -> list[SkillRecord]:
        values = {
            "skill_id": skill_id,
            "alias": alias,
            "version_id": version_id,
            "content_hash": content_hash,
        }
        predicates = tuple(
            Predicate(field=field, op="eq", value=value) for field, value in values.items() if value is not None
        )
        filters = FilterGroup(operator="and", clauses=predicates) if predicates else None
        records = await _query_all(
            database,
            SKILL_TABLE,
            filters=filters,
            sort=(Sort(field="created_at"), Sort(field="version_id")),
        )
        return [from_database_record(record, SkillRecord) for record in records]


async def _query_all(
    database: DatabaseUnitOfWork,
    table: str,
    *,
    filters: FilterGroup | Predicate | None = None,
    sort: tuple[Sort, ...] = (),
) -> list:
    records = []
    cursor: str | None = None
    while True:
        page, cursor = await database.query_records(
            table,
            RecordQuery(filters=filters, sort=sort, page=Page(limit=500, cursor=cursor)),
        )
        records.extend(page)
        if cursor is None:
            return records


__all__ = ["SkillRepository"]
