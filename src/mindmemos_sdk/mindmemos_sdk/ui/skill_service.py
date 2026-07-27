"""UI-facing application service for centralized local Skill management."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..skills import (
    EvolveCloudResult,
    ExportSkillRequest,
    ExportSkillResult,
    LocalSkillManifest,
    LocalSyncOperation,
    PromoteCloudResult,
    PublishLocalRequest,
    PublishLocalResult,
    RegisterLocalRequest,
    RegisterLocalResult,
    SkillEvolveMode,
    SkillManager,
)
from ..skills.bundle import bundle_files_from_content
from ..skills.models import LocalSkillFileRole, LocalSkillSyncState, LocalSkillVersionMetadata


class SkillListItemView(BaseModel):
    """One centralized Skill family row rendered by the local UI."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str
    alias: str | None = None
    cloud_skill_id: str | None = None
    active_version_id: str
    published_head_id: str | None = None
    cloud_revision: int | None = None
    version_count: int
    pending_count: int
    sync_state: str
    last_sync_at: str | None = None


class SkillVersionView(BaseModel):
    """One immutable version row rendered by the local UI."""

    model_config = ConfigDict(extra="forbid")

    version_id: str
    parent_version_id: str | None = None
    version_label: str | None = None
    commit_message: str | None = None
    content_hash: str
    local_snapshot_hash: str
    origin: str
    status: str
    is_active: bool
    is_published: bool
    has_linked_files: bool
    sync_state: str
    created_at: str


class SkillDetailView(BaseModel):
    """Complete local UI detail aggregate for one Skill family."""

    model_config = ConfigDict(extra="forbid")

    skill: SkillListItemView
    versions: list[SkillVersionView] = Field(default_factory=list)
    active_version: SkillVersionView
    published_version: SkillVersionView | None = None
    outbox_operations: list[LocalSyncOperation] = Field(default_factory=list)


class SkillContentView(BaseModel):
    """Human-editable algorithm content for one immutable version."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version_id: str
    content: str


class SkillCompareView(BaseModel):
    """Local-only comparison including private linked-file path changes."""

    model_config = ConfigDict(extra="forbid")

    from_version_id: str
    to_version_id: str
    content_diff: str
    linked_file_changes: list[str] = Field(default_factory=list)


class LocalSkillUIService:
    """Aggregate UI DTOs while delegating every state change to ``SkillManager``."""

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    def list_skills(self) -> list[SkillListItemView]:
        """Return centralized Skill rows without any external source paths."""

        operations = self._manager.local_repository.load_outbox().operations
        return [self._list_item(manifest, operations) for manifest in self._manager.list_local()]

    def detail(self, skill_ref: str) -> SkillDetailView:
        """Build one Skill detail aggregate from immutable local state."""

        manifest = self._manager.show_local(skill_ref)
        operations = [
            operation
            for operation in self._manager.local_repository.load_outbox().operations
            if operation.skill_id == manifest.skill_id
        ]
        versions = [
            self._version_view(manifest, metadata)
            for metadata in self._manager.local_history(manifest.skill_id)
        ]
        by_id = {version.version_id: version for version in versions}
        return SkillDetailView(
            skill=self._list_item(manifest, operations),
            versions=versions,
            active_version=by_id[manifest.active_version_id],
            published_version=by_id.get(manifest.published_head_id or ""),
            outbox_operations=operations,
        )

    def content(self, skill_ref: str, version_id: str | None = None) -> SkillContentView:
        """Return only the algorithm-managed ``SKILL.md`` content."""

        manifest = self._manager.show_local(skill_ref)
        resolved_version_id = version_id or manifest.active_version_id
        snapshot = self._manager.get_local_snapshot(manifest.skill_id, version_id=resolved_version_id)
        return SkillContentView(
            skill_id=manifest.skill_id,
            version_id=resolved_version_id,
            content=bundle_files_from_content(snapshot.content)["SKILL.md"],
        )

    def register(self, request: RegisterLocalRequest) -> RegisterLocalResult:
        """Import a one-time source snapshot through the shared manager."""

        return self._manager.register_local(request)

    def publish(self, request: PublishLocalRequest) -> tuple[PublishLocalResult, SkillDetailView]:
        """Create an immutable editor or directory version and return refreshed detail."""

        result = self._manager.publish_local(request)
        return result, self.detail(result.skill_id)

    def switch(self, skill_ref: str, version_id: str) -> SkillDetailView:
        """Switch the local active pointer and return refreshed detail."""

        manifest = self._manager.switch_local(skill_ref, version_id=version_id)
        return self.detail(manifest.skill_id)

    def export(self, request: ExportSkillRequest) -> ExportSkillResult:
        """Export a complete selected snapshot through the shared manager."""

        return self._manager.export_local(request)

    def sync(self, skill_ref: str, *, direction: str = "both") -> SkillDetailView:
        """Run one explicit cloud direction and return refreshed local state."""

        if direction == "both":
            manifest = self._manager.sync_local(skill_ref)
        elif direction == "pull":
            self._manager.pull_local(skill_ref)
            manifest = self._manager.show_local(skill_ref)
        elif direction == "push":
            manifest = self._manager.show_local(skill_ref)
            for version in self._manager.local_history(manifest.skill_id):
                if version.sync_state == LocalSkillSyncState.CONFLICT:
                    raise ValueError(
                        f"cannot push unresolved conflicting version: {version.version_id}"
                    )
                if version.sync_state != LocalSkillSyncState.SYNCED:
                    self._manager.push_local(
                        manifest.skill_id,
                        version_id=version.version_id,
                    )
            manifest = self._manager.show_local(manifest.skill_id)
        else:
            raise ValueError("sync direction must be 'push', 'pull', or 'both'")
        return self.detail(manifest.skill_id)

    def evolve(
        self,
        skill_ref: str,
        *,
        base_version_id: str | None = None,
        algorithm: str | None = None,
        mode: SkillEvolveMode = "sync",
        operation_id: str | None = None,
    ) -> EvolveCloudResult:
        """Request cloud evolution through the shared manager."""

        return self._manager.evolve_local(
            skill_ref,
            base_version_id=base_version_id,
            algorithm=algorithm,
            mode=mode,
            operation_id=operation_id,
        )

    def promote(
        self,
        skill_ref: str,
        *,
        version_id: str,
        expected_cloud_revision: int | None = None,
        operation_id: str | None = None,
    ) -> PromoteCloudResult:
        """Promote one cloud UUID without changing the local active pointer."""

        return self._manager.promote_local(
            skill_ref,
            version_id=version_id,
            operation_id=operation_id,
            expected_cloud_revision=expected_cloud_revision,
        )

    def compare(self, skill_ref: str, from_version_id: str, to_version_id: str) -> SkillCompareView:
        """Compare algorithm content and local-only linked file manifests."""

        result = self._manager.diff_local(
            skill_ref,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
        )
        manifest = self._manager.show_local(skill_ref)
        from_snapshot = self._manager.get_local_snapshot(manifest.skill_id, version_id=from_version_id)
        to_snapshot = self._manager.get_local_snapshot(manifest.skill_id, version_id=to_version_id)
        from_files = {
            item.path: item.blob_hash
            for item in from_snapshot.files
            if item.role != LocalSkillFileRole.ALGORITHM
        }
        to_files = {
            item.path: item.blob_hash
            for item in to_snapshot.files
            if item.role != LocalSkillFileRole.ALGORITHM
        }
        changed = [
            path
            for path in sorted(set(from_files) | set(to_files))
            if from_files.get(path) != to_files.get(path)
        ]
        return SkillCompareView(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            content_diff=result.diff,
            linked_file_changes=changed,
        )

    def _list_item(
        self,
        manifest: LocalSkillManifest,
        operations: list[LocalSyncOperation],
    ) -> SkillListItemView:
        pending_count = sum(1 for operation in operations if operation.skill_id == manifest.skill_id)
        version_states = {
            version.sync_state
            for version in self._manager.local_history(manifest.skill_id)
        }
        if LocalSkillSyncState.CONFLICT in version_states:
            sync_state = LocalSkillSyncState.CONFLICT.value
        elif pending_count:
            sync_state = LocalSkillSyncState.PENDING.value
        elif manifest.cloud_skill_id is None:
            sync_state = LocalSkillSyncState.LOCAL_ONLY.value
        else:
            sync_state = LocalSkillSyncState.SYNCED.value
        return SkillListItemView(
            skill_id=manifest.skill_id,
            name=manifest.name,
            alias=manifest.alias,
            cloud_skill_id=manifest.cloud_skill_id,
            active_version_id=manifest.active_version_id,
            published_head_id=manifest.published_head_id,
            cloud_revision=manifest.cloud_revision,
            version_count=len(manifest.version_ids),
            pending_count=pending_count,
            sync_state=sync_state,
            last_sync_at=manifest.last_sync_at,
        )

    def _version_view(
        self,
        manifest: LocalSkillManifest,
        metadata: LocalSkillVersionMetadata,
    ) -> SkillVersionView:
        snapshot = self._manager.get_local_snapshot(manifest.skill_id, version_id=metadata.version_id)
        return SkillVersionView(
            version_id=metadata.version_id,
            parent_version_id=metadata.parent_version_id,
            version_label=metadata.version_label,
            commit_message=metadata.commit_message,
            content_hash=metadata.content_hash,
            local_snapshot_hash=metadata.local_snapshot_hash,
            origin=metadata.origin.value,
            status=metadata.cloud_status.value if metadata.cloud_status is not None else "local_only",
            is_active=metadata.version_id == manifest.active_version_id,
            is_published=metadata.version_id == manifest.published_head_id,
            has_linked_files=bool(snapshot.linked_files),
            sync_state=metadata.sync_state.value,
            created_at=metadata.created_at,
        )
