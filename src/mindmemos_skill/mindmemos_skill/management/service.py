"""Standalone local management use cases built on :class:`SkillRepository`."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path

from ..errors import SkillConflictError
from ..persistence import (
    DEFAULT_SKILL_DATABASE_PATH,
    SkillFamilyStateRecord,
    SkillRecord,
    SkillVersionOrigin,
    SkillVersionStatus,
    bootstrap_skill_database,
)
from .bundle import frontmatter_value, next_version_label, parse_version_label, serialize_files
from .installer import SkillInstaller
from .models import (
    DuplicateAction,
    ExportSkillRequest,
    ExportSkillResult,
    ManagedSkill,
    PublishSkillRequest,
    PublishSkillResult,
    RegisterSkillRequest,
    RegisterSkillResult,
    SkillDetail,
    SkillDiffResult,
    SkillSnapshot,
)
from .repository import SkillRepository
from .snapshot import read_skill_snapshot, snapshot_from_editor, snapshot_from_record, snapshot_metadata

_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class LocalSkillManager:
    """Complete local register/publish/query/pointer/export workflow."""

    def __init__(
        self,
        repository: SkillRepository,
        *,
        managed_root: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        id_generator: Callable[[], str] | None = None,
        owns_database: bool = False,
    ) -> None:
        self.repository = repository
        self._installer = SkillInstaller(managed_root=managed_root)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_generator = id_generator or (lambda: str(uuid.uuid4()))
        self._owns_database = owns_database

    @classmethod
    async def open(cls, database_path: str | Path | None = None) -> LocalSkillManager:
        path = DEFAULT_SKILL_DATABASE_PATH if database_path is None else Path(database_path).expanduser()
        database = await bootstrap_skill_database(path)
        return cls(
            SkillRepository(database),
            managed_root=path.parent,
            owns_database=True,
        )

    async def close(self) -> None:
        if self._owns_database:
            await self.repository.database.close()
            self._owns_database = False

    async def register(self, request: RegisterSkillRequest) -> RegisterSkillResult:
        snapshot = read_skill_snapshot(request.source_path)
        matches = await self.repository.find_snapshot_matches(snapshot.local_snapshot_hash)
        if matches and request.duplicate_action is None:
            raise SkillConflictError(
                "identical local Skill snapshot already exists; choose duplicate_action='reuse' or 'create_new'"
            )
        if matches and request.duplicate_action == DuplicateAction.REUSE:
            matched = matches[0]
            state = await self.repository.get_family_state(matched.skill_id)
            return RegisterSkillResult(
                action="reused",
                skill_id=matched.skill_id,
                version_id=matched.version_id,
                effective_version_id=state.effective_version_id,
            )

        now = self._clock()
        skill_id = self._id_generator()
        version_id = self._id_generator()
        alias = _normalize_alias(request.alias)
        name = request.name or frontmatter_value(snapshot.blob["SKILL.md"], "name") or Path(request.source_path).stem
        version_label = request.version_label or frontmatter_value(snapshot.blob["SKILL.md"], "version") or "0.1.0"
        parse_version_label(version_label)
        record = self._record_from_snapshot(
            snapshot,
            skill_id=skill_id,
            version_id=version_id,
            name=name,
            alias=alias,
            version_label=version_label,
            commit_message=_normalized_message(request.commit_message),
            parent_version_ids=[],
            created_at=now,
        )
        state = await self.repository.create_version(
            record,
            now=now,
            pending_operation=_push_operation(skill_id, version_id, now),
        )
        return RegisterSkillResult(
            action="created",
            skill_id=skill_id,
            version_id=version_id,
            effective_version_id=state.effective_version_id,
        )

    async def publish(self, request: PublishSkillRequest) -> PublishSkillResult:
        if (request.source_path is None) == (request.content is None):
            raise SkillConflictError("publish requires exactly one of source_path or content")
        skill_id = await self.repository.resolve_skill_id(request.skill_ref)
        state = await self.repository.get_family_state(skill_id)
        base_version_id = request.base_version_id or state.effective_version_id
        base = await self.repository.get_version(base_version_id)
        if base.skill_id != skill_id:
            raise SkillConflictError(f"version {base_version_id} does not belong to Skill {skill_id}")
        inherited = snapshot_from_record(base)
        snapshot = (
            read_skill_snapshot(request.source_path)
            if request.source_path is not None
            else snapshot_from_editor(request.content or "", inherited)
        )
        existing = await self.repository.list_versions(skill_id)
        version_label = (
            request.version_label
            or frontmatter_value(snapshot.blob["SKILL.md"], "version")
            or next_version_label([item.version_label for item in existing])
        )
        parse_version_label(version_label)
        now = self._clock()
        version_id = self._id_generator()
        record = self._record_from_snapshot(
            snapshot,
            skill_id=skill_id,
            version_id=version_id,
            name=base.name,
            alias=base.alias,
            version_label=version_label,
            commit_message=_normalized_message(request.commit_message),
            parent_version_ids=[base_version_id],
            created_at=now,
            cloud_skill_id=base.cloud_skill_id,
        )
        updated = await self.repository.create_version(
            record,
            now=now,
            make_effective=request.activate,
            expected_effective_version_id=state.effective_version_id if request.activate else None,
            pending_operation=_push_operation(skill_id, version_id, now),
        )
        return PublishSkillResult(
            skill_id=skill_id,
            version_id=version_id,
            effective_version_id=updated.effective_version_id,
            local_snapshot_hash=snapshot.local_snapshot_hash,
        )

    async def list_skills(self) -> list[ManagedSkill]:
        states = await self.repository.list_family_states()
        summaries = [await self._summary(state) for state in states]
        return sorted(summaries, key=lambda item: (item.name.lower(), item.skill_id))

    async def get_skill(self, skill_ref: str) -> SkillDetail:
        skill_id = await self.repository.resolve_skill_id(skill_ref)
        state = await self.repository.get_family_state(skill_id)
        return SkillDetail(
            skill=await self._summary(state),
            effective_version=await self.repository.get_version(state.effective_version_id),
            state=state,
        )

    async def list_versions(self, skill_ref: str) -> list[SkillRecord]:
        return await self.repository.list_versions(skill_ref)

    async def get_version(self, skill_ref: str, version_id: str) -> SkillRecord:
        skill_id = await self.repository.resolve_skill_id(skill_ref)
        record = await self.repository.get_version(version_id)
        if record.skill_id != skill_id:
            raise SkillConflictError(f"version {version_id} does not belong to Skill {skill_id}")
        return record

    async def set_effective_version(
        self,
        skill_ref: str,
        version_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> SkillDetail:
        await self.repository.set_effective_version(
            skill_ref,
            version_id=version_id,
            expected_version_id=expected_version_id,
            updated_at=self._clock(),
        )
        return await self.get_skill(skill_ref)

    async def rollback(
        self,
        skill_ref: str,
        version_id: str,
        *,
        expected_version_id: str | None = None,
    ) -> SkillDetail:
        return await self.set_effective_version(
            skill_ref,
            version_id,
            expected_version_id=expected_version_id,
        )

    async def export(self, request: ExportSkillRequest) -> ExportSkillResult:
        detail = await self.get_skill(request.skill_ref)
        version_id = request.version_id or detail.state.effective_version_id
        record = await self.get_version(detail.skill.skill_id, version_id)
        snapshot = snapshot_from_record(record)
        target = self._installer.export(snapshot, request.target_path, replace=request.replace)
        return ExportSkillResult(
            skill_id=record.skill_id,
            version_id=record.version_id,
            target_path=str(target),
            exported_files=[item.path for item in snapshot.files],
            local_snapshot_hash=snapshot.local_snapshot_hash,
        )

    async def diff(
        self,
        skill_ref: str,
        *,
        to_version_id: str,
        from_version_id: str | None = None,
    ) -> SkillDiffResult:
        detail = await self.get_skill(skill_ref)
        resolved_from = from_version_id or detail.state.effective_version_id
        before = snapshot_from_record(await self.get_version(detail.skill.skill_id, resolved_from))
        after = snapshot_from_record(await self.get_version(detail.skill.skill_id, to_version_id))
        chunks: list[str] = []
        changed_files: list[str] = []
        before_files = before.file_contents
        after_files = after.file_contents
        for path in sorted(set(before_files) | set(after_files)):
            if before_files.get(path) == after_files.get(path):
                continue
            changed_files.append(path)
            chunks.extend(
                unified_diff(
                    before_files.get(path, "").splitlines(keepends=True),
                    after_files.get(path, "").splitlines(keepends=True),
                    fromfile=f"{resolved_from}/{path}",
                    tofile=f"{to_version_id}/{path}",
                )
            )
        return SkillDiffResult(
            skill_id=detail.skill.skill_id,
            from_version_id=resolved_from,
            to_version_id=to_version_id,
            diff="".join(chunks),
            changed_files=changed_files,
        )

    async def _summary(self, state: SkillFamilyStateRecord) -> ManagedSkill:
        versions = await self.repository.query_versions(skill_id=state.skill_id)
        effective = next(item for item in versions if item.version_id == state.effective_version_id)
        return ManagedSkill(
            skill_id=state.skill_id,
            name=effective.name,
            alias=effective.alias,
            cloud_skill_id=effective.cloud_skill_id,
            effective_version_id=state.effective_version_id,
            published_head_id=state.published_head_id,
            cloud_revision=state.cloud_revision,
            last_sync_at=state.last_sync_at,
            version_count=len(versions),
            pending_count=len(state.pending_operations),
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

    @staticmethod
    def _record_from_snapshot(
        snapshot: SkillSnapshot,
        *,
        skill_id: str,
        version_id: str,
        name: str,
        alias: str | None,
        version_label: str,
        commit_message: str | None,
        parent_version_ids: list[str],
        created_at: datetime,
        cloud_skill_id: str | None = None,
    ) -> SkillRecord:
        return SkillRecord(
            skill_id=skill_id,
            version_id=version_id,
            cloud_skill_id=cloud_skill_id,
            parent_version_ids=parent_version_ids,
            name=name,
            description=frontmatter_value(snapshot.blob["SKILL.md"], "description"),
            alias=alias,
            blob=serialize_files(snapshot.blob),
            resources=serialize_files(snapshot.resources),
            content_hash=snapshot.content_hash,
            status=SkillVersionStatus.DRAFT,
            version_label=version_label,
            commit_message=commit_message,
            metadata={"snapshot": snapshot_metadata(snapshot)},
            created_at=created_at,
            origin=SkillVersionOrigin.LOCAL,
        )


def _normalize_alias(alias: str | None) -> str | None:
    if alias is None or not alias.strip():
        return None
    normalized = alias.strip()
    if _ALIAS_PATTERN.fullmatch(normalized) is None:
        raise SkillConflictError(
            "Skill alias must be 1-64 characters and contain only letters, numbers, '.', '_', or '-'"
        )
    return normalized


def _normalized_message(message: str | None) -> str | None:
    normalized = message.strip() if message is not None else ""
    return normalized or None


def _push_operation(skill_id: str, version_id: str, now: datetime) -> dict:
    return {
        "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mindmemos:push:{skill_id}:{version_id}")),
        "operation_type": "push_version",
        "skill_id": skill_id,
        "version_id": version_id,
        "status": "pending",
        "attempt_count": 0,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


__all__ = ["LocalSkillManager"]
