"""High-level SDK skill manager.

This layer coordinates local registry/history state with the cloud skill API.
Checkout operations delegate local file replacement to ``SkillInstaller``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path

from ..config import ConfigManager
from ..errors import MindMemOSSDKError, SkillRegistryError
from .bundle import (
    bundle_files_from_content,
    compute_content_hash,
    read_local_bundle,
    resolve_skill_dir,
    serialize_bundle,
)
from .cloud import SkillCloudClient
from .history import SkillHistoryStore
from .installer import SkillInstaller
from .local_repository import LocalSkillRepository
from .models import (
    EvolveCloudRequest,
    EvolveCloudResult,
    ExportSkillRequest,
    ExportSkillResult,
    HashState,
    LocalSkillManifest,
    LocalSkillSnapshot,
    LocalSkillSyncState,
    LocalSkillVersion,
    LocalSkillVersionMetadata,
    PromoteCloudRequest,
    PromoteCloudResult,
    PublishLocalRequest,
    PublishLocalResult,
    PullVersionSummary,
    PushVersionRequest,
    PushVersionResult,
    RegisterLocalRequest,
    RegisterLocalResult,
    RollbackPlan,
    SkillCheckoutPlan,
    SkillContext,
    SkillDiffResult,
    SkillEvolveMode,
    SkillFlushResult,
    SkillOrigin,
    SkillPendingUpload,
    SkillRecord,
    SkillSyncRequestItem,
    SkillUpdateResult,
    SkillUsage,
    SyncCloudItem,
    SyncCloudRequest,
)
from .pending import SkillPendingUploadStore
from .registry import SkillRegistry


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillRegisterPlan:
    """Small plain object used while preparing a skill registration."""

    def __init__(
        self,
        *,
        path: str,
        skill_name: str,
        version_label: str | None,
        content_hash: str,
        base_version_id: str,
    ) -> None:
        self.path = path
        self.skill_name = skill_name
        self.version_label = version_label
        self.content_hash = content_hash
        self.base_version_id = base_version_id


class SkillManager:
    """Coordinate SDK-managed skills across registry, history, cache and cloud."""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        history: SkillHistoryStore,
        pending: SkillPendingUploadStore,
        cloud: SkillCloudClient | None,
        installer: SkillInstaller,
        local_repository: LocalSkillRepository | None = None,
    ) -> None:
        self._registry = registry
        self._history = history
        self._pending = pending
        self._cloud = cloud
        self._installer = installer
        self._local_repository = local_repository

    @classmethod
    def from_config_manager(
        cls,
        config_manager: ConfigManager,
        cloud: SkillCloudClient | None = None,
    ) -> SkillManager:
        """Create a manager using SDK local stores and an optional API backend.

        A missing cloud backend disables only remote operations. Centralized
        local version management remains fully available, including in Lite mode.
        """

        local_repository = LocalSkillRepository(config_manager)
        local_repository.recover_outbox()
        return cls(
            registry=SkillRegistry(config_manager),
            history=SkillHistoryStore(config_manager),
            pending=SkillPendingUploadStore(config_manager),
            cloud=cloud,
            installer=SkillInstaller(config_manager.load_or_default().storage.skill_backup_dir),
            local_repository=local_repository,
        )

    def _cloud_client(self) -> SkillCloudClient:
        if self._cloud is None:
            raise SkillRegistryError(
                "this SkillManager has no MindMemOS API backend; local version operations remain available"
            )
        return self._cloud

    @property
    def local_repository(self) -> LocalSkillRepository:
        """Return the centralized local repository used by the new control plane."""

        if self._local_repository is None:
            raise SkillRegistryError("centralized local Skill repository is not configured")
        return self._local_repository

    def register_local(self, request: RegisterLocalRequest) -> RegisterLocalResult:
        """Import one explicit source snapshot without reading it again at runtime."""

        return self.local_repository.register(request)

    def publish_local(self, request: PublishLocalRequest) -> PublishLocalResult:
        """Create one immutable local version without implicitly publishing it in cloud."""

        return self.local_repository.publish(request)

    def list_local(self) -> list[LocalSkillManifest]:
        """List Skill families managed by the centralized local repository."""

        return self.local_repository.list_manifests()

    def show_local(self, skill_ref: str) -> LocalSkillManifest:
        """Resolve one centralized local Skill family by UUID or alias."""

        return self.local_repository.get_manifest(skill_ref)

    def local_history(self, skill_ref: str) -> list[LocalSkillVersionMetadata]:
        """Return immutable local versions in manifest order."""

        return self.local_repository.list_versions(skill_ref)

    def get_local_snapshot(self, skill_ref: str, *, version_id: str | None = None) -> LocalSkillSnapshot:
        """Load the active or selected complete snapshot from ``~/.mindmemos``."""

        return self.local_repository.read_snapshot(skill_ref, version_id)

    def switch_local(self, skill_ref: str, *, version_id: str) -> LocalSkillManifest:
        """Switch only the local active pointer."""

        return self.local_repository.switch(skill_ref, version_id)

    def rollback_local(self, skill_ref: str, *, version_id: str) -> LocalSkillManifest:
        """Apply rollback semantics by switching only the local active pointer."""

        return self.switch_local(skill_ref, version_id=version_id)

    def export_local(self, request: ExportSkillRequest) -> ExportSkillResult:
        """Export a complete version without creating or activating another version."""

        return self.local_repository.export(request)

    def diff_local(
        self,
        skill_ref: str,
        *,
        from_version_id: str | None = None,
        to_version_id: str,
    ) -> SkillDiffResult:
        """Compare two centralized immutable versions without changing active state."""

        manifest = self.show_local(skill_ref)
        resolved_from = from_version_id or manifest.active_version_id
        from_snapshot = self.get_local_snapshot(manifest.skill_id, version_id=resolved_from)
        to_snapshot = self.get_local_snapshot(manifest.skill_id, version_id=to_version_id)
        return SkillDiffResult(
            skill_id=manifest.skill_id,
            from_version_id=resolved_from,
            to_version_id=to_version_id,
            diff=self._diff_contents(
                from_snapshot.content,
                to_snapshot.content,
                resolved_from,
                to_version_id,
            ),
        )

    def active_skill_context(
        self,
        skill_ref: str,
        *,
        usage: SkillUsage | str | None = None,
    ) -> SkillContext:
        """Build trace context from the centralized active version only."""

        manifest = self.show_local(skill_ref)
        version = self.local_repository.get_version(manifest.skill_id, manifest.active_version_id)
        return SkillContext(
            name=manifest.name,
            content_hash=version.content_hash,
            version_id=version.version_id,
            version_label=version.version_label,
            usage=usage,
        )

    def push_local(self, skill_ref: str, *, version_id: str | None = None) -> PushVersionResult:
        """Push one immutable local UUID version and never upload private snapshot fields."""

        manifest = self.show_local(skill_ref)
        resolved_version_id = version_id or manifest.active_version_id
        metadata = self.local_repository.get_version(manifest.skill_id, resolved_version_id)
        snapshot = self.get_local_snapshot(manifest.skill_id, version_id=resolved_version_id)
        operation = self.local_repository.enqueue_push(manifest.skill_id, resolved_version_id)
        self.local_repository.mark_outbox_running(operation.operation_id)
        request = PushVersionRequest(
            operation_id=operation.operation_id,
            cloud_skill_id=manifest.cloud_skill_id,
            name=manifest.name,
            version_id=metadata.version_id,
            parent_version_id=metadata.parent_version_id,
            content=snapshot.content,
            expected_content_hash=metadata.content_hash,
            version_label=metadata.version_label,
            commit_message=metadata.commit_message,
            created_at=metadata.created_at,
        )
        try:
            result = self._cloud_client().push_version(request)
        except Exception as exc:
            self.local_repository.mark_outbox_failed(
                operation.operation_id,
                error_code=type(exc).__name__,
            )
            if isinstance(exc, MindMemOSSDKError):
                raise
            raise SkillRegistryError(f"failed to push local Skill version: {exc}") from exc
        if (
            result.version_id != metadata.version_id
            or result.content_hash != metadata.content_hash
            or result.created_at != metadata.created_at
        ):
            self.local_repository.mark_outbox_failed(
                operation.operation_id,
                error_code="immutable_response_mismatch",
            )
            raise SkillRegistryError(
                f"cloud Push response changed immutable version fields: {metadata.version_id}"
            )
        self.local_repository.mark_version_synced(
            manifest.skill_id,
            version_id=metadata.version_id,
            cloud_skill_id=result.cloud_skill_id,
            cloud_status=result.status,
        )
        self.local_repository.remove_outbox_operation(operation.operation_id)
        return result

    def pull_local(self, skill_ref: str) -> list[LocalSkillVersionMetadata]:
        """Pull cloud versions parent-first without changing the local active pointer."""

        manifest = self.show_local(skill_ref)
        if manifest.cloud_skill_id is None:
            raise SkillRegistryError(f"local Skill has no cloud mapping: {manifest.skill_id}")
        imported: list[LocalSkillVersionMetadata] = []
        summaries = self._order_pull_summaries(
            manifest,
            self._pull_version_summaries(manifest.cloud_skill_id),
        )
        for summary in summaries:
            content = self._cloud_client().pull_content(manifest.cloud_skill_id, summary.version_id)
            if content.version != summary:
                raise SkillRegistryError(
                    f"cloud version metadata changed while pulling: {summary.version_id}"
                )
            imported.append(
                self.local_repository.import_cloud_version(
                    manifest.skill_id,
                    summary=summary,
                    content=content.content,
                )
            )
        return imported

    def sync_local(self, skill_ref: str) -> LocalSkillManifest:
        """Push pending versions, Pull missing versions, then atomically cache cloud pointers."""

        manifest = self.show_local(skill_ref)
        for version_id in manifest.version_ids:
            metadata = self.local_repository.get_version(manifest.skill_id, version_id)
            if metadata.sync_state == LocalSkillSyncState.CONFLICT:
                raise SkillRegistryError(
                    f"cannot sync unresolved conflicting version: {version_id}"
                )
            if metadata.sync_state != LocalSkillSyncState.SYNCED:
                self.push_local(manifest.skill_id, version_id=version_id)
        manifest = self.show_local(manifest.skill_id)
        if manifest.cloud_skill_id is None:
            raise SkillRegistryError(f"cloud mapping was not established for {manifest.skill_id}")
        result = self._cloud_client().sync_cloud(
            SyncCloudRequest(
                items=[
                    SyncCloudItem(
                        cloud_skill_id=manifest.cloud_skill_id,
                        known_version_ids=list(manifest.version_ids),
                        known_published_head_id=manifest.published_head_id,
                        known_cloud_revision=manifest.cloud_revision,
                    )
                ]
            )
        )
        item = next(
            (candidate for candidate in result.items if candidate.cloud_skill_id == manifest.cloud_skill_id),
            None,
        )
        if item is None:
            raise SkillRegistryError(f"cloud Sync omitted Skill family: {manifest.cloud_skill_id}")
        for summary in self._order_pull_summaries(manifest, item.versions):
            content = self._cloud_client().pull_content(manifest.cloud_skill_id, summary.version_id)
            if content.version != summary:
                raise SkillRegistryError(
                    f"cloud version metadata changed while syncing: {summary.version_id}"
                )
            self.local_repository.import_cloud_version(
                manifest.skill_id,
                summary=summary,
                content=content.content,
            )
        return self.local_repository.complete_sync(
            manifest.skill_id,
            published_head_id=item.published_head_id,
            cloud_revision=item.cloud_revision,
        )

    def flush_local_outbox(
        self,
    ) -> list[PushVersionResult | PromoteCloudResult]:
        """Retry all centralized pending/failed Push operations in stable order."""

        results: list[PushVersionResult | PromoteCloudResult] = []
        operations = list(self.local_repository.load_outbox().operations)
        for operation in operations:
            if operation.version_id is None:
                continue
            if operation.operation_type.value == "push_version":
                results.append(
                    self.push_local(
                        operation.skill_id,
                        version_id=operation.version_id,
                    )
                )
            elif (
                operation.operation_type.value == "promote"
                and operation.expected_cloud_revision is not None
            ):
                results.append(
                    self.promote_local(
                        operation.skill_id,
                        version_id=operation.version_id,
                        operation_id=operation.operation_id,
                        expected_cloud_revision=operation.expected_cloud_revision,
                    )
                )
        return results

    def ensure_active_cloud_version(self, skill_ref: str) -> PushVersionResult | None:
        """Ensure the active UUID version exists in cloud before it is used in a Trace."""

        manifest = self.show_local(skill_ref)
        metadata = self.local_repository.get_version(
            manifest.skill_id,
            manifest.active_version_id,
        )
        if (
            manifest.cloud_skill_id is not None
            and metadata.sync_state == LocalSkillSyncState.SYNCED
        ):
            return None
        return self.push_local(
            manifest.skill_id,
            version_id=manifest.active_version_id,
        )

    def promote_local(
        self,
        skill_ref: str,
        *,
        version_id: str,
        operation_id: str | None = None,
        expected_cloud_revision: int | None = None,
    ) -> PromoteCloudResult:
        """Promote one cloud version without changing the local active pointer."""

        manifest = self.show_local(skill_ref)
        if manifest.cloud_skill_id is None:
            raise SkillRegistryError(f"local Skill has no cloud mapping: {manifest.skill_id}")
        if manifest.cloud_revision is None:
            raise SkillRegistryError("cloud revision is unknown; run Skill sync before promote")
        if (
            expected_cloud_revision is not None
            and expected_cloud_revision != manifest.cloud_revision
        ):
            raise SkillRegistryError(
                "local cloud revision changed before promote; refresh and retry"
            )
        self.local_repository.get_version(manifest.skill_id, version_id)
        operation = self.local_repository.enqueue_promote(
            manifest.skill_id,
            version_id,
            expected_cloud_revision=manifest.cloud_revision,
            operation_id=operation_id,
        )
        self.local_repository.mark_outbox_running(operation.operation_id)
        try:
            result = self._cloud_client().promote(
                manifest.cloud_skill_id,
                PromoteCloudRequest(
                    operation_id=operation.operation_id,
                    version_id=version_id,
                    expected_cloud_revision=manifest.cloud_revision,
                ),
            )
        except Exception as exc:
            self.local_repository.mark_outbox_failed(
                operation.operation_id,
                error_code=type(exc).__name__,
            )
            if isinstance(exc, MindMemOSSDKError):
                raise
            raise SkillRegistryError(
                f"failed to promote cloud Skill version: {exc}"
            ) from exc
        if (
            result.cloud_skill_id != manifest.cloud_skill_id
            or result.published_head_id != version_id
        ):
            self.local_repository.mark_outbox_failed(
                operation.operation_id,
                error_code="promote_response_mismatch",
            )
            raise SkillRegistryError(
                "cloud Promote response changed the requested Skill or version"
            )
        self.local_repository.update_cloud_pointer(
            manifest.skill_id,
            published_head_id=result.published_head_id,
            cloud_revision=result.cloud_revision,
        )
        self.local_repository.remove_outbox_operation(operation.operation_id)
        return result

    def evolve_local(
        self,
        skill_ref: str,
        *,
        base_version_id: str | None = None,
        algorithm: str | None = None,
        mode: SkillEvolveMode = "sync",
        operation_id: str | None = None,
    ) -> EvolveCloudResult:
        """Request cloud evolution from an explicit local UUID baseline."""

        manifest = self.show_local(skill_ref)
        if manifest.cloud_skill_id is None:
            raise SkillRegistryError(f"local Skill has no cloud mapping: {manifest.skill_id}")
        resolved_base = base_version_id or manifest.active_version_id
        self.local_repository.get_version(manifest.skill_id, resolved_base)
        return self._cloud_client().evolve_cloud(
            EvolveCloudRequest(
                operation_id=operation_id or str(uuid.uuid4()),
                cloud_skill_id=manifest.cloud_skill_id,
                base_version_id=resolved_base,
                algorithm=algorithm,
                mode=mode,
            )
        )

    def _pull_version_summaries(self, cloud_skill_id: str) -> list[PullVersionSummary]:
        summaries: list[PullVersionSummary] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = self._cloud_client().pull_versions(cloud_skill_id, cursor=cursor)
            summaries.extend(page.versions)
            if page.next_cursor is None:
                return summaries
            if page.next_cursor in seen_cursors:
                raise SkillRegistryError(f"cloud version pagination cursor repeated: {page.next_cursor}")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    @staticmethod
    def _order_pull_summaries(
        manifest: LocalSkillManifest,
        summaries: list[PullVersionSummary],
    ) -> list[PullVersionSummary]:
        """Return unique missing cloud versions in parent-first order."""

        by_id: dict[str, PullVersionSummary] = {}
        for summary in summaries:
            existing = by_id.get(summary.version_id)
            if existing is not None and existing != summary:
                raise SkillRegistryError(
                    f"cloud returned conflicting metadata for version: {summary.version_id}"
                )
            by_id[summary.version_id] = summary

        available = set(manifest.version_ids)
        ordered: list[PullVersionSummary] = sorted(
            (
                summary
                for version_id, summary in by_id.items()
                if version_id in available
            ),
            key=lambda summary: (summary.received_at, summary.version_id),
        )
        pending = {
            version_id: summary
            for version_id, summary in by_id.items()
            if version_id not in available
        }
        while pending:
            ready = sorted(
                (
                    summary
                    for summary in pending.values()
                    if summary.parent_version_id is None or summary.parent_version_id in available
                ),
                key=lambda summary: (summary.received_at, summary.version_id),
            )
            if not ready:
                unresolved = ", ".join(sorted(pending))
                raise SkillRegistryError(
                    f"cloud version graph has missing parents or a cycle: {unresolved}"
                )
            for summary in ready:
                ordered.append(summary)
                available.add(summary.version_id)
                pending.pop(summary.version_id)
        return ordered

    def plan_register(
        self,
        path: str,
        *,
        name: str | None = None,
        version_label: str | None = None,
        alias: str | None = None,
    ) -> SkillRegisterPlan:
        """Build a local upload plan without changing state or calling the cloud."""

        skill_dir = str(resolve_skill_dir(path).resolve())
        files = read_local_bundle(skill_dir)
        skill_name, manifest_version = _parse_skill_metadata(files["SKILL.md"])
        existing = self._registry.get_by_path(skill_dir)
        self._registry.ensure_alias_available(alias, skill_id=existing.skill_id if existing else None, path=skill_dir)
        return SkillRegisterPlan(
            path=skill_dir,
            skill_name=name or skill_name or Path(skill_dir).name,
            version_label=version_label or manifest_version,
            content_hash=compute_content_hash(files),
            base_version_id=existing.base_version_id if existing else "",
        )

    def register(
        self,
        path: str,
        *,
        name: str | None = None,
        version_label: str | None = None,
        alias: str | None = None,
    ) -> SkillRecord:
        """Register/upload a local skill directory and persist local state."""

        plan = self.plan_register(path, name=name, version_label=version_label, alias=alias)

        files = read_local_bundle(plan.path)
        content = serialize_bundle(files)
        response = self._cloud_client().register(
            name=plan.skill_name,
            content=content,
            version_label=plan.version_label,
            parent_version_id=plan.base_version_id or None,
        )
        now = _utc_now_iso()
        record = SkillRecord(
            path=plan.path,
            alias=alias,
            skill_name=plan.skill_name,
            cloud_skill_id=response.cloud_skill_id,
            base_version_id=response.version_id,
            content_hash=response.content_hash,
            hash_state=HashState.CONFIRMED,
            version_label=response.version_label or plan.version_label,
            registered_at=now,
            updated_at=now,
        )
        saved = self._registry.upsert(record)
        self._history.upsert_versions(
            response.cloud_skill_id,
            skill_name=plan.skill_name,
            versions=[
                LocalSkillVersion(
                    version_id=response.version_id,
                    parent_version_id=plan.base_version_id or None,
                    version_label=response.version_label or plan.version_label,
                    status=response.status,
                    origin=SkillOrigin.EDGE,
                    content_hash=response.content_hash,
                    created_at=now,
                )
            ],
        )
        self._history.write_cached_content(response.content_hash, content)
        return saved

    def enqueue_pending_upload(self, skill_id: str, *, version_label: str | None = None) -> SkillPendingUpload:
        """Snapshot current local content and enqueue a non-blocking upload retry."""

        record = self.show(skill_id)
        files = read_local_bundle(record.path)
        content = serialize_bundle(files)
        content_hash = compute_content_hash(files)
        now = _utc_now_iso()
        upload = SkillPendingUpload(
            job_id=_pending_job_id(record.skill_id, record.base_version_id, content_hash),
            skill_id=record.skill_id,
            path=record.path,
            skill_name=record.skill_name,
            cloud_skill_id=record.cloud_skill_id,
            parent_version_id=record.base_version_id,
            content_hash=content_hash,
            version_label=version_label if version_label is not None else record.version_label,
            content_cache_key=content_hash,
            created_at=now,
            updated_at=now,
        )
        self._history.write_cached_content(content_hash, content)
        self._pending.upsert(upload)
        self._registry.upsert(
            record.model_copy(
                update={
                    "content_hash": content_hash,
                    "hash_state": HashState.PENDING_UPLOAD,
                    "updated_at": now,
                }
            )
        )
        return upload

    def ensure_skill_context(self, skill_id: str, *, usage: SkillUsage | str | None = None) -> SkillContext:
        """Return context for the centralized active version."""

        return self.active_skill_context(skill_id, usage=usage)

    def ensure_legacy_skill_context(
        self,
        skill_id: str,
        *,
        usage: SkillUsage | str | None = None,
    ) -> SkillContext:
        """Explicit compatibility path for pre-centralized registry records."""

        record = self.show(skill_id)
        current_hash = _current_content_hash(record.path)
        if current_hash is None:
            raise SkillRegistryError(f"cannot read local skill bundle: {record.path}")

        if record.hash_state != HashState.CONFIRMED or record.content_hash != current_hash:
            upload = self.enqueue_pending_upload(skill_id)
            return SkillContext(
                name=upload.skill_name,
                content_hash=upload.content_hash,
                version_id=upload.parent_version_id,
                version_label=upload.version_label,
                usage=usage,
            )

        return SkillContext(
            name=record.skill_name,
            content_hash=record.content_hash,
            version_id=record.base_version_id,
            version_label=record.version_label,
            usage=usage,
        )

    def pending_uploads(self) -> list[SkillPendingUpload]:
        """Return local pending skill upload jobs."""

        return self._pending.list()

    def flush_pending_uploads(self, *, limit: int | None = None) -> list[SkillFlushResult]:
        """Retry pending uploads from the local outbox.

        Successful uploads always enter history. The registry only advances when
        the current local files still match the uploaded snapshot; if the user has
        edited the skill again, that newer content remains pending for a later job.
        """

        uploads = self._pending.list()
        if limit is not None:
            uploads = uploads[:limit]

        results: list[SkillFlushResult] = []
        for upload in uploads:
            result = self._flush_one(upload)
            results.append(result)
        return results

    def _flush_one(self, upload: SkillPendingUpload) -> SkillFlushResult:
        now = _utc_now_iso()
        content = self._history.read_cached_content(upload.content_cache_key)
        if content is None:
            error = f"missing cached skill content: {upload.content_cache_key}"
            self._pending.mark_failed(upload.job_id, error=error, updated_at=now)
            return SkillFlushResult(
                skill_id=upload.skill_id,
                content_hash=upload.content_hash,
                parent_version_id=upload.parent_version_id,
                uploaded=False,
                error=error,
            )

        try:
            response = self._cloud_client().register(
                name=upload.skill_name,
                content=content,
                version_label=upload.version_label,
                parent_version_id=upload.parent_version_id or None,
            )
        except Exception as exc:
            error = str(exc)
            self._pending.mark_failed(upload.job_id, error=error, updated_at=now)
            return SkillFlushResult(
                skill_id=upload.skill_id,
                content_hash=upload.content_hash,
                parent_version_id=upload.parent_version_id,
                uploaded=False,
                error=error,
            )

        cloud_skill_id = upload.cloud_skill_id or response.cloud_skill_id
        self._history.upsert_versions(
            cloud_skill_id,
            skill_name=upload.skill_name,
            versions=[
                LocalSkillVersion(
                    version_id=response.version_id,
                    parent_version_id=upload.parent_version_id or None,
                    version_label=response.version_label or upload.version_label,
                    status=response.status,
                    origin=SkillOrigin.EDGE,
                    content_hash=response.content_hash,
                    created_at=now,
                )
            ],
        )
        self._history.write_cached_content(response.content_hash, content)
        self._pending.remove(upload.job_id)

        registry_advanced = False
        record = self._registry.get_by_skill_id(upload.skill_id)
        if record is not None and _current_content_hash(record.path) == upload.content_hash:
            self._registry.upsert(
                record.model_copy(
                    update={
                        "cloud_skill_id": cloud_skill_id,
                        "base_version_id": response.version_id,
                        "content_hash": response.content_hash,
                        "hash_state": HashState.CONFIRMED,
                        "version_label": response.version_label or upload.version_label,
                        "updated_at": now,
                    }
                )
            )
            registry_advanced = True

        return SkillFlushResult(
            skill_id=upload.skill_id,
            content_hash=upload.content_hash,
            parent_version_id=upload.parent_version_id,
            uploaded=True,
            version_id=response.version_id,
            registry_advanced=registry_advanced,
        )

    def list(self) -> list[SkillRecord]:
        """Return SDK-managed skills."""

        return self._registry.list()

    @property
    def registry(self) -> SkillRegistry:
        """Return the local skill registry used by this manager."""

        return self._registry

    def show(self, skill_ref: str) -> SkillRecord:
        """Return one managed skill or raise."""

        record = self._registry.get_by_ref(skill_ref)
        if record is None:
            raise SkillRegistryError(f"skill not found: {skill_ref}")
        return record

    def skill_id_for_context(self, context: SkillContext) -> str | None:
        """Find a managed skill matching a detected context."""

        return next(
            (
                manifest.skill_id
                for manifest in self.list_local()
                if manifest.name == context.name
            ),
            None,
        )

    def legacy_skill_id_for_context(self, context: SkillContext) -> str | None:
        """Explicitly resolve one pre-centralized path-registry record."""

        return next(
            (
                record.skill_id
                for record in self.list()
                if record.skill_name == context.name
            ),
            None,
        )

    def pull(self, skill_id: str) -> list[LocalSkillVersion]:
        """Pull cloud version metadata into local history without changing files."""

        record = self.show(skill_id)
        if not record.cloud_skill_id:
            raise SkillRegistryError(f"skill has no cloud_skill_id yet: {skill_id}")
        entry = self._history.get(record.cloud_skill_id)
        versions = self._cloud_client().versions_since(
            record.cloud_skill_id,
            since=entry.last_pulled_at if entry else None,
        )
        local_versions = [LocalSkillVersion.from_cloud(version) for version in versions]
        self._history.upsert_versions(
            record.cloud_skill_id,
            skill_name=record.skill_name,
            versions=local_versions,
            last_pulled_at=_utc_now_iso(),
        )
        return local_versions

    def sync(self) -> list[SkillUpdateResult]:
        """Check registered skills for published cloud heads."""

        records = [record for record in self.list() if record.cloud_skill_id and record.base_version_id]
        if not records:
            return []
        data = self._cloud_client().sync(
            [
                SkillSyncRequestItem(
                    cloud_skill_id=record.cloud_skill_id or "",
                    local_version_id=record.base_version_id,
                )
                for record in records
            ]
        )
        by_cloud = {record.cloud_skill_id: record for record in records}
        results: list[SkillUpdateResult] = []
        for item in data.results:
            record = by_cloud.get(item.cloud_skill_id)
            if record is None:
                continue
            if item.published_head is not None:
                self._history.upsert_versions(
                    item.cloud_skill_id,
                    skill_name=record.skill_name,
                    versions=[LocalSkillVersion.from_cloud(item.published_head)],
                    last_pulled_at=_utc_now_iso(),
                )
            results.append(
                SkillUpdateResult(
                    skill_id=record.skill_id,
                    skill_name=record.skill_name,
                    had_update=item.has_update and item.published_head is not None,
                    message=item.gating_status,
                )
            )
        return results

    def history(self, skill_id: str) -> list[LocalSkillVersion]:
        """Return local version history for a managed skill."""

        record = self.show(skill_id)
        if not record.cloud_skill_id:
            return []
        entry = self._history.get(record.cloud_skill_id)
        return entry.versions if entry else []

    def get_content(self, skill_id: str, *, version_id: str | None = None) -> str:
        """Return canonical bundle content for the local copy or one history version."""

        record = self.show(skill_id)
        if version_id is None or version_id == record.base_version_id:
            return serialize_bundle(read_local_bundle(record.path))
        return self._ensure_version_content(record, version_id)

    def save_content(self, skill_id: str, *, content: str) -> SkillRecord:
        """Atomically save a bare ``SKILL.md`` or canonical bundle to the local copy."""

        record = self.show(skill_id)
        files = bundle_files_from_content(content)
        root = resolve_skill_dir(record.path)
        if not root.is_dir():
            raise SkillRegistryError(f"skill directory does not exist: {root}")

        target = root / "SKILL.md"
        fd, temporary_path = tempfile.mkstemp(dir=root, prefix=".SKILL.md-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(files["SKILL.md"])
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise
        return self.show(skill_id)

    def plan_rollback(self, skill_id: str, *, version_id: str) -> RollbackPlan:
        """Build a rollback checkout plan without changing local files."""

        record = self.show(skill_id)
        content = self._ensure_version_content(record, version_id)
        return self._installer.plan_checkout(record, target_version_id=version_id, content=content)

    def plan_update(self, skill_id: str) -> SkillCheckoutPlan | None:
        """Build an update plan to the current published head, if one exists."""

        record = self.show(skill_id)
        current_hash = _current_content_hash(record.path)
        if current_hash is not None and record.content_hash and current_hash != record.content_hash:
            raise SkillRegistryError(
                f"local skill has unuploaded changes: {record.path}. "
                f"Run `mindmemos skill push {record.skill_id}` before update, or rollback explicitly."
            )
        target = self._published_head_for_record(record)
        if target is None or target.version_id == record.base_version_id:
            return None
        content = self._ensure_version_content(record, target.version_id)
        return self._installer.plan_checkout(record, target_version_id=target.version_id, content=content)

    def push(self, skill_id: str, *, version_label: str | None = None) -> SkillRecord:
        """Upload current local skill content as a new edge version."""

        record = self.show(skill_id)
        current_hash = _current_content_hash(record.path)
        if current_hash is None:
            raise SkillRegistryError(f"cannot read local skill bundle: {record.path}")
        if record.hash_state == HashState.CONFIRMED and record.content_hash == current_hash:
            return record
        self.enqueue_pending_upload(skill_id, version_label=version_label)
        results = self.flush_pending_uploads(limit=1)
        if not results or not results[0].uploaded or not results[0].registry_advanced:
            error = results[0].error if results else "upload did not complete"
            raise SkillRegistryError(f"failed to push local skill changes: {error}")
        return self.show(skill_id)

    def update(self, skill_id: str) -> SkillUpdateResult:
        """Update one managed skill to its published head when available."""

        record = self.show(skill_id)
        plan = self.plan_update(skill_id)
        if plan is None:
            return SkillUpdateResult(
                skill_id=record.skill_id,
                skill_name=record.skill_name,
                had_update=False,
                message="already up to date",
            )
        updated = self.apply_checkout(plan)
        return SkillUpdateResult(
            skill_id=updated.skill_id,
            skill_name=updated.skill_name,
            had_update=True,
            plan=plan,
            record=updated,
        )

    def update_all(self) -> list[SkillUpdateResult]:
        """Update every managed skill that has a published cloud head."""

        return [self.update(record.skill_id) for record in self.list()]

    def apply_checkout(self, plan: SkillCheckoutPlan) -> SkillRecord:
        """Apply a prepared checkout plan and advance the registry after success."""

        record = self.show(plan.skill_id)
        if record.cloud_skill_id is None:
            raise SkillRegistryError(f"skill has no cloud_skill_id yet: {plan.skill_id}")
        version = self._find_history_version(record.cloud_skill_id, plan.to_version_id)
        if version is None:
            raise SkillRegistryError(f"unknown skill version: {plan.to_version_id}")
        content = self._ensure_version_content(record, plan.to_version_id)
        self._installer.apply_checkout(plan, content=content)
        return self._registry.upsert(
            record.model_copy(
                update={
                    "base_version_id": version.version_id,
                    "content_hash": version.content_hash,
                    "hash_state": HashState.CONFIRMED,
                    "version_label": version.version_label,
                    "updated_at": _utc_now_iso(),
                }
            )
        )

    def rollback(self, skill_id: str, *, version_id: str) -> tuple[RollbackPlan, SkillRecord]:
        """Roll back one managed skill to a known version."""

        plan = self.plan_rollback(skill_id, version_id=version_id)
        record = self.apply_checkout(plan)
        return plan, record

    def diff(self, skill_id: str, *, from_version_id: str | None = None, to_version_id: str) -> SkillDiffResult:
        """Return a unified text diff between two skill versions without changing files."""

        record = self.show(skill_id)
        from_version_id = from_version_id or record.base_version_id
        from_content = self._ensure_version_content(record, from_version_id)
        to_content = self._ensure_version_content(record, to_version_id)
        diff_text = self._diff_contents(from_content, to_content, from_version_id, to_version_id)
        return SkillDiffResult(
            skill_id=record.skill_id,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            diff=diff_text,
        )

    def unregister(self, skill_id: str) -> SkillRecord:
        """Remove a skill from local SDK management and cloud management relation."""

        record = self.show(skill_id)
        if record.cloud_skill_id:
            self._cloud_client().delete_skill(record.cloud_skill_id)
            self._history.remove(record.cloud_skill_id)
        removed = self._registry.remove(skill_id=record.skill_id)
        if removed is None:
            raise SkillRegistryError(f"skill not found: {skill_id}")
        return removed

    def _ensure_version_content(self, record: SkillRecord, version_id: str) -> str:
        if record.cloud_skill_id is None:
            raise SkillRegistryError(f"skill has no cloud_skill_id yet: {record.skill_id}")
        version = self._find_history_version(record.cloud_skill_id, version_id)
        if version is None:
            self.pull(record.skill_id)
            version = self._find_history_version(record.cloud_skill_id, version_id)
        if version is None:
            raise SkillRegistryError(f"unknown skill version: {version_id}")

        content = self._history.read_cached_content(version.content_hash)
        if content is not None and compute_content_hash(bundle_files_from_content(content)) == version.content_hash:
            return content

        downloaded = self._cloud_client().get_content(record.cloud_skill_id, version.version_id)
        downloaded_hash = compute_content_hash(bundle_files_from_content(downloaded.content))
        if downloaded_hash != downloaded.version.content_hash or downloaded_hash != version.content_hash:
            raise SkillRegistryError(
                f"downloaded content hash mismatch for {version.version_id}: "
                f"expected {version.content_hash}, got {downloaded_hash}"
            )
        self._history.upsert_versions(
            record.cloud_skill_id,
            skill_name=record.skill_name,
            versions=[LocalSkillVersion.from_cloud(downloaded.version)],
        )
        self._history.write_cached_content(downloaded.version.content_hash, downloaded.content)
        return downloaded.content

    def _find_history_version(self, cloud_skill_id: str, version_id: str) -> LocalSkillVersion | None:
        entry = self._history.get(cloud_skill_id)
        if entry is None:
            return None
        return next((version for version in entry.versions if version.version_id == version_id), None)

    def _published_head_for_record(self, record: SkillRecord) -> LocalSkillVersion | None:
        if record.cloud_skill_id is None:
            raise SkillRegistryError(f"skill has no cloud_skill_id yet: {record.skill_id}")
        summary = self._cloud_client().get_skill(record.cloud_skill_id)
        head = summary.published_head or summary.latest_version
        if head is None:
            return None
        local = LocalSkillVersion.from_cloud(head)
        self._history.upsert_versions(
            record.cloud_skill_id,
            skill_name=record.skill_name,
            versions=[local],
            last_pulled_at=_utc_now_iso(),
        )
        return local

    @staticmethod
    def _diff_contents(from_content: str, to_content: str, from_version_id: str, to_version_id: str) -> str:
        from_files = bundle_files_from_content(from_content)
        to_files = bundle_files_from_content(to_content)
        chunks: list[str] = []
        for path in sorted(set(from_files) | set(to_files)):
            chunks.extend(
                unified_diff(
                    from_files.get(path, "").splitlines(keepends=True),
                    to_files.get(path, "").splitlines(keepends=True),
                    fromfile=f"{from_version_id}/{path}",
                    tofile=f"{to_version_id}/{path}",
                )
            )
        return "".join(chunks)


def _parse_skill_metadata(content: str) -> tuple[str | None, str | None]:
    """Best-effort parse of ``name`` and ``version`` from SKILL.md frontmatter."""

    name = _find_simple_field(content, "name")
    version = _find_simple_field(content, "version")
    return name, version


def _find_simple_field(content: str, field: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*[\"']?([^\"'\n#]+)", re.MULTILINE)
    match = pattern.search(content)
    return match.group(1).strip() if match else None


def _pending_job_id(skill_id: str, parent_version_id: str, content_hash: str) -> str:
    digest = hashlib.sha256(f"{skill_id}\0{parent_version_id}\0{content_hash}".encode("utf-8")).hexdigest()[:24]
    return f"spu_{digest}"


def _current_content_hash(path: str) -> str | None:
    try:
        return compute_content_hash(read_local_bundle(path))
    except Exception:
        return None
