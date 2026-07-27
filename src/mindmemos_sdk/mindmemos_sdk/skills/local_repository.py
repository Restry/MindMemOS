"""Centralized immutable local Skill repository.

``LocalSkillRepository`` owns the on-disk state below the SDK configuration
directory. External directories are explicit import/export boundaries and are
never retained as runtime content sources.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from ..config import ConfigManager
from ..errors import LocalSkillRepositoryError
from .models import (
    DuplicateSkillAction,
    DuplicateSkillMatch,
    ExportSkillRequest,
    ExportSkillResult,
    LocalSkillFileEntry,
    LocalSkillFileRole,
    LocalSkillManifest,
    LocalSkillOperationStatus,
    LocalSkillOperationType,
    LocalSkillSnapshot,
    LocalSkillSyncState,
    LocalSkillVersionFiles,
    LocalSkillVersionMetadata,
    LocalSyncOperation,
    LocalSyncOutbox,
    PublishLocalRequest,
    PublishLocalResult,
    PullVersionSummary,
    RegisterLocalRequest,
    RegisterLocalResult,
    SkillOrigin,
    SkillVersionStatus,
)
from .registry import _normalize_alias
from .snapshot import (
    read_local_snapshot,
    snapshot_from_cloud_content,
    snapshot_from_editor,
    validate_snapshot_path,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a supported SDK target yet.
    fcntl = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalized_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = message.strip()
    return normalized or None


class LocalSkillRepository:
    """Persist immutable Skill versions, full snapshots, pointers and outbox state."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.root = config_manager.config_dir
        self.skills_root = self.root / "skills"
        self.bundle_blobs_root = self.root / "blobs" / "bundles"
        self.file_blobs_root = self.root / "blobs" / "files"
        self.locks_root = self.root / "locks"
        self.outbox_path = self.root / "outbox.json"

    def list_manifests(self) -> list[LocalSkillManifest]:
        """Return all valid local Skill family manifests."""

        if not self.skills_root.is_dir():
            return []
        manifests = [
            self._read_manifest(path.parent.name)
            for path in sorted(self.skills_root.glob("*/manifest.json"))
            if path.is_file()
        ]
        return sorted(manifests, key=lambda item: (item.name.lower(), item.skill_id))

    def get_manifest(self, skill_ref: str) -> LocalSkillManifest:
        """Resolve one local Skill by UUID or alias."""

        direct = self.skills_root / skill_ref / "manifest.json"
        if direct.is_file():
            return self._read_model(direct, LocalSkillManifest)
        matches = [manifest for manifest in self.list_manifests() if manifest.alias == skill_ref]
        if not matches:
            raise LocalSkillRepositoryError(f"local skill not found: {skill_ref}")
        if len(matches) > 1:
            raise LocalSkillRepositoryError(f"local skill alias is ambiguous: {skill_ref}")
        return matches[0]

    def get_version(self, skill_ref: str, version_id: str) -> LocalSkillVersionMetadata:
        """Read immutable version metadata and verify family membership."""

        manifest = self.get_manifest(skill_ref)
        if version_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(f"version {version_id} does not belong to skill {manifest.skill_id}")
        metadata = self._read_model(self._metadata_path(manifest.skill_id, version_id), LocalSkillVersionMetadata)
        if metadata.skill_id != manifest.skill_id or metadata.version_id != version_id:
            raise LocalSkillRepositoryError(f"invalid version identity at {version_id}")
        return metadata

    def list_versions(self, skill_ref: str) -> list[LocalSkillVersionMetadata]:
        """Return the complete immutable version history in manifest order."""

        manifest = self.get_manifest(skill_ref)
        return [self.get_version(manifest.skill_id, version_id) for version_id in manifest.version_ids]

    def read_snapshot(self, skill_ref: str, version_id: str | None = None) -> LocalSkillSnapshot:
        """Restore and validate one complete snapshot from local blobs."""

        manifest = self.get_manifest(skill_ref)
        resolved_version_id = version_id or manifest.active_version_id
        metadata = self.get_version(manifest.skill_id, resolved_version_id)
        files_manifest = self._read_model(
            self._files_path(manifest.skill_id, resolved_version_id), LocalSkillVersionFiles
        )
        if files_manifest.version_id != resolved_version_id:
            raise LocalSkillRepositoryError(f"invalid files manifest for version {resolved_version_id}")

        bundle_path = self.bundle_blobs_root / metadata.content_hash / "content"
        if not bundle_path.is_file():
            raise LocalSkillRepositoryError(f"missing bundle blob: {metadata.content_hash}")
        content = bundle_path.read_text(encoding="utf-8")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != metadata.content_hash:
            # The SDK content hash is over canonical bundle JSON. This direct hash
            # is intentionally not used as the authoritative validation below.
            from .bundle import bundle_files_from_content, compute_content_hash

            if compute_content_hash(bundle_files_from_content(content)) != metadata.content_hash:
                raise LocalSkillRepositoryError(f"corrupt bundle blob: {metadata.content_hash}")

        file_contents: dict[str, str] = {}
        for entry in files_manifest.files:
            relative = validate_snapshot_path(entry.path)
            blob_path = self.file_blobs_root / entry.blob_hash / "content"
            if not blob_path.is_file():
                raise LocalSkillRepositoryError(f"missing file blob for {relative}: {entry.blob_hash}")
            raw = blob_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.blob_hash:
                raise LocalSkillRepositoryError(f"corrupt file blob for {relative}: {entry.blob_hash}")
            try:
                file_contents[relative] = raw.decode(entry.encoding)
            except UnicodeDecodeError as exc:
                raise LocalSkillRepositoryError(f"invalid UTF-8 file blob for {relative}") from exc

        snapshot = LocalSkillSnapshot(
            content=content,
            content_hash=metadata.content_hash,
            local_snapshot_hash=metadata.local_snapshot_hash,
            files=files_manifest.files,
            file_contents=file_contents,
        )
        from .snapshot import compute_local_snapshot_hash

        actual_snapshot_hash = compute_local_snapshot_hash(snapshot.content_hash, snapshot.files)
        if actual_snapshot_hash != metadata.local_snapshot_hash:
            raise LocalSkillRepositoryError(
                f"local snapshot hash mismatch for {resolved_version_id}: "
                f"expected {metadata.local_snapshot_hash}, got {actual_snapshot_hash}"
            )
        return snapshot

    def find_snapshot_matches(self, local_snapshot_hash: str) -> list[DuplicateSkillMatch]:
        """Find existing versions with the same complete local snapshot."""

        matches: list[DuplicateSkillMatch] = []
        for manifest in self.list_manifests():
            for version_id in manifest.version_ids:
                metadata = self.get_version(manifest.skill_id, version_id)
                if metadata.local_snapshot_hash != local_snapshot_hash:
                    continue
                matches.append(
                    DuplicateSkillMatch(
                        local_snapshot_hash=local_snapshot_hash,
                        skill_id=manifest.skill_id,
                        name=manifest.name,
                        active_version_id=manifest.active_version_id,
                        matched_version_id=version_id,
                        cloud_skill_id=manifest.cloud_skill_id,
                        last_sync_at=manifest.last_sync_at,
                    )
                )
        return matches

    def register(self, request: RegisterLocalRequest) -> RegisterLocalResult:
        """Import an external directory as a new local Skill family or reuse a match."""

        snapshot = read_local_snapshot(request.source_path)
        with self._file_lock(self.locks_root / "skills.lock"):
            return self._register_snapshot(request, snapshot)

    def _register_snapshot(
        self,
        request: RegisterLocalRequest,
        snapshot: LocalSkillSnapshot,
    ) -> RegisterLocalResult:
        """Commit one registration while the global family/alias lock is held."""

        matches = self.find_snapshot_matches(snapshot.local_snapshot_hash)
        if matches and request.duplicate_action is None:
            raise LocalSkillRepositoryError(
                "identical local Skill snapshot already exists; choose duplicate_action='reuse' or 'create_new'"
            )
        if matches and request.duplicate_action == DuplicateSkillAction.REUSE:
            match = matches[0]
            return RegisterLocalResult(
                action="reused",
                skill_id=match.skill_id,
                version_id=match.matched_version_id,
                active_version_id=match.active_version_id,
                summary=match,
            )

        alias = self._ensure_alias_available(request.alias)
        skill_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        now = _utc_now_iso()
        name = request.name or _snapshot_skill_name(snapshot) or Path(request.source_path).expanduser().stem
        metadata = LocalSkillVersionMetadata(
            version_id=version_id,
            skill_id=skill_id,
            skill_name=name,
            content_hash=snapshot.content_hash,
            local_snapshot_hash=snapshot.local_snapshot_hash,
            version_label=request.version_label or _snapshot_version_label(snapshot),
            commit_message=_normalized_message(request.commit_message),
            origin=SkillOrigin.EDGE,
            sync_state=LocalSkillSyncState.PENDING,
            created_at=now,
        )
        manifest = LocalSkillManifest(
            skill_id=skill_id,
            name=name,
            alias=alias,
            active_version_id=version_id,
            version_ids=[version_id],
            created_at=now,
            updated_at=now,
        )
        with self._skill_lock(skill_id):
            self._persist_snapshot(snapshot)
            self._write_version(metadata, snapshot.files)
            self._atomic_write_model(self._manifest_path(skill_id), manifest)
        self.enqueue_push(skill_id, version_id)
        return RegisterLocalResult(
            action="created",
            skill_id=skill_id,
            version_id=version_id,
            active_version_id=version_id,
        )

    def publish(self, request: PublishLocalRequest) -> PublishLocalResult:
        """Create a new immutable child version without implicitly activating it."""

        manifest = self.get_manifest(request.skill_id)
        base_version_id = request.base_version_id or manifest.active_version_id
        base_metadata = self.get_version(manifest.skill_id, base_version_id)
        if request.source_path is not None and request.content is not None:
            raise LocalSkillRepositoryError("publish accepts either source_path or content, not both")
        if request.source_path is None and request.content is None:
            raise LocalSkillRepositoryError("publish requires source_path or content")
        if request.source_path is not None:
            snapshot = read_local_snapshot(request.source_path)
        else:
            inherited = self.read_snapshot(manifest.skill_id, base_version_id)
            snapshot = snapshot_from_editor(content=request.content or "", inherited_snapshot=inherited)

        version_id = str(uuid.uuid4())
        now = _utc_now_iso()
        metadata = LocalSkillVersionMetadata(
            version_id=version_id,
            skill_id=manifest.skill_id,
            parent_version_id=base_metadata.version_id,
            skill_name=manifest.name,
            content_hash=snapshot.content_hash,
            local_snapshot_hash=snapshot.local_snapshot_hash,
            version_label=request.version_label,
            commit_message=_normalized_message(request.commit_message),
            origin=SkillOrigin.EDGE,
            sync_state=LocalSkillSyncState.PENDING,
            created_at=now,
        )
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if base_version_id not in latest.version_ids:
                raise LocalSkillRepositoryError(f"publish base version disappeared: {base_version_id}")
            self._persist_snapshot(snapshot)
            self._write_version(metadata, snapshot.files)
            updated = latest.model_copy(
                update={
                    "version_ids": [*latest.version_ids, version_id],
                    "active_version_id": version_id if request.activate else latest.active_version_id,
                    "updated_at": now,
                }
            )
            self._atomic_write_model(self._manifest_path(manifest.skill_id), updated)
        self.enqueue_push(manifest.skill_id, version_id)
        return PublishLocalResult(
            skill_id=manifest.skill_id,
            version_id=version_id,
            active_version_id=updated.active_version_id,
            local_snapshot_hash=snapshot.local_snapshot_hash,
        )

    def switch(self, skill_ref: str, version_id: str) -> LocalSkillManifest:
        """Atomically change only the local active version pointer."""

        manifest = self.get_manifest(skill_ref)
        if version_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(f"version {version_id} does not belong to skill {manifest.skill_id}")
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if version_id not in latest.version_ids:
                raise LocalSkillRepositoryError(f"version {version_id} does not belong to skill {manifest.skill_id}")
            updated = latest.model_copy(update={"active_version_id": version_id, "updated_at": _utc_now_iso()})
            self._atomic_write_model(self._manifest_path(manifest.skill_id), updated)
        return updated

    def import_cloud_version(
        self,
        skill_ref: str,
        *,
        summary: PullVersionSummary,
        content: str,
    ) -> LocalSkillVersionMetadata:
        """Persist one cloud UUID version without changing the local active pointer."""

        from .bundle import bundle_files_from_content, compute_content_hash

        manifest = self.get_manifest(skill_ref)
        actual_content_hash = compute_content_hash(bundle_files_from_content(content))
        if actual_content_hash != summary.content_hash:
            raise LocalSkillRepositoryError(
                f"cloud content hash mismatch for {summary.version_id}: "
                f"expected {summary.content_hash}, got {actual_content_hash}"
            )
        if summary.parent_version_id is not None and summary.parent_version_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(
                f"cloud parent version must be imported first: {summary.parent_version_id}"
            )

        if summary.version_id in manifest.version_ids:
            existing = self.get_version(manifest.skill_id, summary.version_id)
            immutable_cloud_fields = (
                existing.parent_version_id,
                existing.content_hash,
                existing.version_label,
                existing.commit_message,
                existing.origin,
                existing.created_at,
            )
            incoming_cloud_fields = (
                summary.parent_version_id,
                summary.content_hash,
                summary.version_label,
                summary.commit_message,
                summary.origin,
                summary.created_at,
            )
            if immutable_cloud_fields != incoming_cloud_fields:
                with self._skill_lock(manifest.skill_id):
                    self._atomic_write_model(
                        self._metadata_path(manifest.skill_id, summary.version_id),
                        existing.model_copy(
                            update={"sync_state": LocalSkillSyncState.CONFLICT}
                        ),
                    )
                raise LocalSkillRepositoryError(
                    f"cloud version conflicts with immutable local version: {summary.version_id}"
                )
            updated = existing.model_copy(
                update={
                    "cloud_status": summary.status,
                    "sync_state": LocalSkillSyncState.SYNCED,
                }
            )
            with self._skill_lock(manifest.skill_id):
                self._atomic_write_model(self._metadata_path(manifest.skill_id, summary.version_id), updated)
            return updated

        inherited = (
            self.read_snapshot(manifest.skill_id, summary.parent_version_id)
            if summary.parent_version_id is not None
            else None
        )
        snapshot = snapshot_from_cloud_content(content=content, inherited_snapshot=inherited)
        if snapshot.content_hash != summary.content_hash:
            raise LocalSkillRepositoryError(
                f"materialized cloud content hash mismatch for {summary.version_id}"
            )
        metadata = LocalSkillVersionMetadata(
            version_id=summary.version_id,
            skill_id=manifest.skill_id,
            parent_version_id=summary.parent_version_id,
            skill_name=manifest.name,
            content_hash=summary.content_hash,
            local_snapshot_hash=snapshot.local_snapshot_hash,
            version_label=summary.version_label,
            commit_message=summary.commit_message,
            origin=summary.origin,
            cloud_status=summary.status,
            sync_state=LocalSkillSyncState.SYNCED,
            created_at=summary.created_at,
        )
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if summary.parent_version_id is not None and summary.parent_version_id not in latest.version_ids:
                raise LocalSkillRepositoryError(
                    f"cloud parent version disappeared during import: {summary.parent_version_id}"
                )
            if summary.version_id in latest.version_ids:
                existing = self._read_model(
                    self._metadata_path(manifest.skill_id, summary.version_id),
                    LocalSkillVersionMetadata,
                )
                if (
                    existing.parent_version_id,
                    existing.content_hash,
                    existing.version_label,
                    existing.commit_message,
                    existing.origin,
                    existing.created_at,
                ) != (
                    summary.parent_version_id,
                    summary.content_hash,
                    summary.version_label,
                    summary.commit_message,
                    summary.origin,
                    summary.created_at,
                ):
                    self._atomic_write_model(
                        self._metadata_path(manifest.skill_id, summary.version_id),
                        existing.model_copy(
                            update={"sync_state": LocalSkillSyncState.CONFLICT}
                        ),
                    )
                    raise LocalSkillRepositoryError(
                        f"cloud version conflicts with concurrent local import: {summary.version_id}"
                    )
                updated_existing = existing.model_copy(
                    update={
                        "cloud_status": summary.status,
                        "sync_state": LocalSkillSyncState.SYNCED,
                    }
                )
                self._atomic_write_model(
                    self._metadata_path(manifest.skill_id, summary.version_id),
                    updated_existing,
                )
                return updated_existing
            self._persist_snapshot(snapshot)
            self._write_version(metadata, snapshot.files)
            self._atomic_write_model(
                self._manifest_path(manifest.skill_id),
                latest.model_copy(
                    update={
                        "version_ids": [*latest.version_ids, summary.version_id],
                        "updated_at": _utc_now_iso(),
                    }
                ),
            )
        return metadata

    def mark_version_synced(
        self,
        skill_ref: str,
        *,
        version_id: str,
        cloud_skill_id: str,
        cloud_status: SkillVersionStatus,
    ) -> LocalSkillManifest:
        """Record a successful Push without changing either active or published pointer."""

        manifest = self.get_manifest(skill_ref)
        if version_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(
                f"version {version_id} does not belong to skill {manifest.skill_id}"
            )
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if latest.cloud_skill_id is not None and latest.cloud_skill_id != cloud_skill_id:
                raise LocalSkillRepositoryError(
                    f"local Skill is already bound to another cloud family: {latest.cloud_skill_id}"
                )
            metadata = self._read_model(
                self._metadata_path(manifest.skill_id, version_id),
                LocalSkillVersionMetadata,
            )
            self._atomic_write_model(
                self._metadata_path(manifest.skill_id, version_id),
                metadata.model_copy(
                    update={
                        "cloud_status": cloud_status,
                        "sync_state": LocalSkillSyncState.SYNCED,
                    }
                ),
            )
            updated = latest.model_copy(
                update={
                    "cloud_skill_id": cloud_skill_id,
                    "updated_at": _utc_now_iso(),
                }
            )
            self._atomic_write_model(self._manifest_path(manifest.skill_id), updated)
        return updated

    def complete_sync(
        self,
        skill_ref: str,
        *,
        published_head_id: str | None,
        cloud_revision: int,
        synced_at: str | None = None,
    ) -> LocalSkillManifest:
        """Commit cloud pointer metadata only after all version imports succeed."""

        manifest = self.get_manifest(skill_ref)
        if published_head_id is not None and published_head_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(
                f"published head is not fully available locally: {published_head_id}"
            )
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if published_head_id is not None and published_head_id not in latest.version_ids:
                raise LocalSkillRepositoryError(
                    f"published head is not fully available locally: {published_head_id}"
                )
            updated = latest.model_copy(
                update={
                    "published_head_id": published_head_id,
                    "cloud_revision": cloud_revision,
                    "last_sync_at": synced_at or _utc_now_iso(),
                    "updated_at": _utc_now_iso(),
                }
            )
            self._atomic_write_model(self._manifest_path(manifest.skill_id), updated)
        return updated

    def update_cloud_pointer(
        self,
        skill_ref: str,
        *,
        published_head_id: str,
        cloud_revision: int,
    ) -> LocalSkillManifest:
        """Cache a confirmed cloud promote result without changing local active or sync time."""

        manifest = self.get_manifest(skill_ref)
        if published_head_id not in manifest.version_ids:
            raise LocalSkillRepositoryError(
                f"published head is not fully available locally: {published_head_id}"
            )
        with self._skill_lock(manifest.skill_id):
            latest = self._read_manifest(manifest.skill_id)
            if published_head_id not in latest.version_ids:
                raise LocalSkillRepositoryError(
                    f"published head is not fully available locally: {published_head_id}"
                )
            updated = latest.model_copy(
                update={
                    "published_head_id": published_head_id,
                    "cloud_revision": cloud_revision,
                    "updated_at": _utc_now_iso(),
                }
            )
            self._atomic_write_model(self._manifest_path(manifest.skill_id), updated)
        return updated

    def export(self, request: ExportSkillRequest) -> ExportSkillResult:
        """Materialize a complete version through a verified temporary directory."""

        manifest = self.get_manifest(request.skill_id)
        version_id = request.version_id or manifest.active_version_id
        metadata = self.get_version(manifest.skill_id, version_id)
        snapshot = self.read_snapshot(manifest.skill_id, version_id)
        target = self._validated_export_target(request.target_path)
        if target.exists() and not request.replace:
            raise LocalSkillRepositoryError(f"export target already exists: {target}")
        if target.exists() and not target.is_dir():
            raise LocalSkillRepositoryError(f"export target is not a directory: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}-export-"))
        backup: Path | None = None
        try:
            for entry in snapshot.files:
                destination = temporary / validate_snapshot_path(entry.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(snapshot.file_contents[entry.path], encoding=entry.encoding)
                if entry.mode is not None:
                    destination.chmod(entry.mode)
            _verify_export(temporary, snapshot)
            if target.exists():
                backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
                os.replace(target, backup)
            os.replace(temporary, target)
        except BaseException:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
        return ExportSkillResult(
            skill_id=manifest.skill_id,
            version_id=version_id,
            target_path=str(target),
            exported_files=[entry.path for entry in snapshot.files],
            local_snapshot_hash=metadata.local_snapshot_hash,
        )

    def load_outbox(self) -> LocalSyncOutbox:
        """Read the centralized retry outbox."""

        if not self.outbox_path.is_file():
            return LocalSyncOutbox()
        return self._read_model(self.outbox_path, LocalSyncOutbox)

    def enqueue_push(self, skill_id: str, version_id: str) -> LocalSyncOperation:
        """Idempotently enqueue one local version for cloud Push."""

        now = _utc_now_iso()
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mindmemos:push:{skill_id}:{version_id}"))
        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            existing = next((item for item in outbox.operations if item.operation_id == operation_id), None)
            if existing is not None:
                return existing
            operation = LocalSyncOperation(
                operation_id=operation_id,
                operation_type=LocalSkillOperationType.PUSH_VERSION,
                skill_id=skill_id,
                version_id=version_id,
                status=LocalSkillOperationStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            outbox.operations.append(operation)
            self._atomic_write_model(self.outbox_path, outbox)
            return operation

    def enqueue_promote(
        self,
        skill_id: str,
        version_id: str,
        *,
        expected_cloud_revision: int,
        operation_id: str | None = None,
    ) -> LocalSyncOperation:
        """Idempotently enqueue one cloud published-pointer CAS operation."""

        now = _utc_now_iso()
        resolved_operation_id = operation_id or str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    f"mindmemos:promote:{skill_id}:{version_id}:"
                    f"{expected_cloud_revision}"
                ),
            )
        )
        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            existing = next(
                (
                    item
                    for item in outbox.operations
                    if item.operation_id == resolved_operation_id
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.operation_type != LocalSkillOperationType.PROMOTE
                    or existing.skill_id != skill_id
                    or existing.version_id != version_id
                    or existing.expected_cloud_revision
                    != expected_cloud_revision
                ):
                    raise LocalSkillRepositoryError(
                        "outbox operation_id already belongs to different immutable inputs"
                    )
                return existing
            operation = LocalSyncOperation(
                operation_id=resolved_operation_id,
                operation_type=LocalSkillOperationType.PROMOTE,
                skill_id=skill_id,
                version_id=version_id,
                expected_cloud_revision=expected_cloud_revision,
                status=LocalSkillOperationStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            outbox.operations.append(operation)
            self._atomic_write_model(self.outbox_path, outbox)
            return operation

    def remove_outbox_operation(self, operation_id: str) -> LocalSyncOperation | None:
        """Remove one confirmed outbox operation under the global outbox lock."""

        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            removed = next(
                (operation for operation in outbox.operations if operation.operation_id == operation_id),
                None,
            )
            if removed is None:
                return None
            outbox.operations = [
                operation for operation in outbox.operations if operation.operation_id != operation_id
            ]
            self._atomic_write_model(self.outbox_path, outbox)
            return removed

    def mark_outbox_running(self, operation_id: str) -> LocalSyncOperation:
        """Lease one operation before a network attempt."""

        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            for index, operation in enumerate(outbox.operations):
                if operation.operation_id != operation_id:
                    continue
                updated = operation.model_copy(
                    update={
                        "status": LocalSkillOperationStatus.RUNNING,
                        "attempt_count": operation.attempt_count + 1,
                        "next_retry_at": None,
                        "last_error_code": None,
                        "updated_at": _utc_now_iso(),
                    }
                )
                outbox.operations[index] = updated
                self._atomic_write_model(self.outbox_path, outbox)
                return updated
        raise LocalSkillRepositoryError(f"outbox operation not found: {operation_id}")

    def mark_outbox_failed(self, operation_id: str, *, error_code: str) -> LocalSyncOperation:
        """Persist a stable retry failure without dropping the operation."""

        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            for index, operation in enumerate(outbox.operations):
                if operation.operation_id != operation_id:
                    continue
                updated = operation.model_copy(
                    update={
                        "status": LocalSkillOperationStatus.FAILED,
                        "next_retry_at": (
                            datetime.now(timezone.utc)
                            + timedelta(
                                seconds=min(
                                    3600,
                                    5 * (2 ** max(0, operation.attempt_count - 1)),
                                )
                            )
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "last_error_code": error_code,
                        "updated_at": _utc_now_iso(),
                    }
                )
                outbox.operations[index] = updated
                self._atomic_write_model(self.outbox_path, outbox)
                return updated
        raise LocalSkillRepositoryError(f"outbox operation not found: {operation_id}")

    def recover_outbox(self, *, lease_seconds: int = 300) -> LocalSyncOutbox:
        """Rebuild missing Push entries and release stale running operations."""

        now = datetime.now(timezone.utc)
        expected: dict[str, tuple[str, str]] = {}
        for manifest in self.list_manifests():
            for version_id in manifest.version_ids:
                metadata = self.get_version(manifest.skill_id, version_id)
                if metadata.sync_state == LocalSkillSyncState.PENDING:
                    operation_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"mindmemos:push:{manifest.skill_id}:{version_id}",
                        )
                    )
                    expected[operation_id] = (manifest.skill_id, version_id)

        with self._file_lock(self.locks_root / "outbox.lock"):
            outbox = self.load_outbox()
            recovered: list[LocalSyncOperation] = []
            seen: set[str] = set()
            for operation in outbox.operations:
                updated = operation
                if operation.status == LocalSkillOperationStatus.RUNNING:
                    updated_at = datetime.fromisoformat(
                        operation.updated_at.replace("Z", "+00:00")
                    )
                    if (now - updated_at).total_seconds() >= lease_seconds:
                        updated = operation.model_copy(
                            update={
                                "status": LocalSkillOperationStatus.PENDING,
                                "updated_at": _utc_now_iso(),
                            }
                        )
                recovered.append(updated)
                seen.add(updated.operation_id)
            for operation_id, (skill_id, version_id) in expected.items():
                if operation_id in seen:
                    continue
                timestamp = _utc_now_iso()
                recovered.append(
                    LocalSyncOperation(
                        operation_id=operation_id,
                        operation_type=LocalSkillOperationType.PUSH_VERSION,
                        skill_id=skill_id,
                        version_id=version_id,
                        status=LocalSkillOperationStatus.PENDING,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            updated_outbox = outbox.model_copy(update={"operations": recovered})
            if updated_outbox != outbox:
                self._atomic_write_model(self.outbox_path, updated_outbox)
            return updated_outbox

    def _persist_snapshot(self, snapshot: LocalSkillSnapshot) -> None:
        self._write_blob(self.bundle_blobs_root / snapshot.content_hash / "content", snapshot.content.encode("utf-8"))
        for entry in snapshot.files:
            self._write_blob(
                self.file_blobs_root / entry.blob_hash / "content",
                snapshot.file_contents[entry.path].encode(entry.encoding),
            )

    def _write_version(
        self,
        metadata: LocalSkillVersionMetadata,
        files: list[LocalSkillFileEntry],
    ) -> None:
        version_dir = self._version_dir(metadata.skill_id, metadata.version_id)
        if version_dir.exists():
            existing = self._read_model(version_dir / "metadata.json", LocalSkillVersionMetadata)
            if existing != metadata:
                raise LocalSkillRepositoryError(f"immutable version already exists with different metadata: {metadata.version_id}")
            return
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(dir=version_dir.parent, prefix=f".{metadata.version_id}-"))
        try:
            self._atomic_write_model(temporary / "metadata.json", metadata)
            self._atomic_write_model(
                temporary / "files.json",
                LocalSkillVersionFiles(
                    version_id=metadata.version_id,
                    algorithm_files=[
                        entry
                        for entry in files
                        if entry.role == LocalSkillFileRole.ALGORITHM
                    ],
                    linked_files=[
                        entry
                        for entry in files
                        if entry.role != LocalSkillFileRole.ALGORITHM
                    ],
                ),
            )
            os.replace(temporary, version_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _write_blob(self, path: Path, payload: bytes) -> None:
        if path.is_file():
            if path.read_bytes() != payload:
                raise LocalSkillRepositoryError(f"blob hash collision or corruption at {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(dir=path.parent, prefix=".blob-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise LocalSkillRepositoryError(f"blob hash collision or corruption at {path}")
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            raise

    def _ensure_alias_available(self, alias: str | None) -> str | None:
        normalized = _normalize_alias(alias)
        if normalized is None:
            return None
        for manifest in self.list_manifests():
            if manifest.alias == normalized or manifest.skill_id == normalized:
                raise LocalSkillRepositoryError(f"local skill alias already exists: {normalized}")
        return normalized

    def _read_manifest(self, skill_id: str) -> LocalSkillManifest:
        return self._read_model(self._manifest_path(skill_id), LocalSkillManifest)

    @staticmethod
    def _read_model(path: Path, model_type):
        if not path.is_file():
            raise LocalSkillRepositoryError(f"missing local Skill state: {path}")
        try:
            return model_type.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as exc:
            raise LocalSkillRepositoryError(f"invalid local Skill state at {path}: {exc}") from exc

    @staticmethod
    def _atomic_write_model(path: Path, model) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = model.model_dump_json(indent=2) + "\n"
        fd, temporary_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            raise

    @contextmanager
    def _skill_lock(self, skill_id: str) -> Iterator[None]:
        with self._file_lock(self.locks_root / f"{skill_id}.lock"):
            yield

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        if fcntl is None:
            raise LocalSkillRepositoryError("cross-process file locking is unavailable on this platform")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _manifest_path(self, skill_id: str) -> Path:
        return self.skills_root / skill_id / "manifest.json"

    def _version_dir(self, skill_id: str, version_id: str) -> Path:
        return self.skills_root / skill_id / "versions" / version_id

    def _metadata_path(self, skill_id: str, version_id: str) -> Path:
        return self._version_dir(skill_id, version_id) / "metadata.json"

    def _files_path(self, skill_id: str, version_id: str) -> Path:
        return self._version_dir(skill_id, version_id) / "files.json"

    def _validated_export_target(self, target_path: str) -> Path:
        expanded = Path(target_path).expanduser()
        if expanded.is_symlink():
            raise LocalSkillRepositoryError(f"export target cannot be a symbolic link: {expanded}")
        target = expanded.resolve()
        filesystem_root = Path(target.anchor)
        home = Path.home().resolve()
        repository_root = self.root.resolve()
        if target in {filesystem_root, home, repository_root}:
            raise LocalSkillRepositoryError(f"refusing unsafe export target: {target}")
        if target.is_relative_to(repository_root) or repository_root.is_relative_to(target):
            raise LocalSkillRepositoryError(f"export target overlaps the local Skill repository: {target}")
        return target


def _snapshot_skill_name(snapshot: LocalSkillSnapshot) -> str | None:
    return _simple_frontmatter_value(snapshot.file_contents["SKILL.md"], "name")


def _snapshot_version_label(snapshot: LocalSkillSnapshot) -> str | None:
    return _simple_frontmatter_value(snapshot.file_contents["SKILL.md"], "version")


def _simple_frontmatter_value(content: str, field: str) -> str | None:
    import re

    match = re.search(rf"^\s*{re.escape(field)}\s*:\s*[\"']?([^\"'\n#]+)", content, re.MULTILINE)
    return match.group(1).strip() if match else None


def _verify_export(root: Path, expected: LocalSkillSnapshot) -> None:
    actual = read_local_snapshot(root)
    if actual.local_snapshot_hash != expected.local_snapshot_hash:
        raise LocalSkillRepositoryError(
            f"export verification failed: expected {expected.local_snapshot_hash}, got {actual.local_snapshot_hash}"
        )
