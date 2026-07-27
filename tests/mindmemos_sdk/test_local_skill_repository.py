"""Tests for the centralized immutable SDK Skill repository."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.errors import LocalSkillRepositoryError, SkillRegistryError, SkillSnapshotError
from mindmemos_sdk.skills import (
    DuplicateSkillAction,
    ExportSkillRequest,
    LocalSkillRepository,
    LocalSkillSyncState,
    PromoteCloudResult,
    PublishLocalRequest,
    PullVersionContent,
    PullVersionSummary,
    PushVersionResult,
    RegisterLocalRequest,
    SkillContext,
    SkillManager,
    SkillVersionStatus,
    SyncCloudResult,
    SyncCloudResultItem,
    read_local_snapshot,
)
from pydantic import ValidationError


def _source(tmp_path: Path, *, body: str = "Body\n") -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_text(f'name: demo\nversion: "1.0.0"\n\n{body}', encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "api.md").write_text("private reference\n", encoding="utf-8")
    (root / "scripts").mkdir()
    script = root / "scripts" / "check.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    script.chmod(0o755)
    return root


def _repository(tmp_path: Path) -> LocalSkillRepository:
    return LocalSkillRepository(ConfigManager(config_dir=tmp_path / "config"))


def test_snapshot_splits_cloud_content_from_private_linked_files(tmp_path):
    source = _source(tmp_path)

    first = read_local_snapshot(source)
    (source / "references" / "api.md").write_text("changed private reference\n", encoding="utf-8")
    second = read_local_snapshot(source)

    assert first.content_hash == second.content_hash
    assert first.local_snapshot_hash != second.local_snapshot_hash
    assert json.loads(first.content) == [
        {
            "content": 'name: demo\nversion: "1.0.0"\n\nBody\n',
            "path": "SKILL.md",
        }
    ]
    assert first.linked_files == {
        "references/api.md": "private reference\n",
        "scripts/check.py": "print('ok')\n",
    }


def test_trace_context_serializes_exact_version_binding_and_forbids_private_fields():
    context = SkillContext(
        name="demo",
        content_hash="a" * 64,
        base_version_id="00000000-0000-4000-8000-000000000011",
        usage="injected",
    )

    assert context.base_version_id == context.version_id
    assert context.model_dump(exclude_none=True) == {
        "name": "demo",
        "content_hash": "a" * 64,
        "version_id": "00000000-0000-4000-8000-000000000011",
        "usage": "injected",
    }
    with pytest.raises(ValidationError):
        SkillContext.model_validate(
            {
                **context.model_dump(),
                "linked_files": {"references/private.md": "secret"},
            }
        )


def test_register_persists_uuid_version_snapshot_blobs_manifest_and_outbox(tmp_path):
    repository = _repository(tmp_path)
    source = _source(tmp_path)

    result = repository.register(
        RegisterLocalRequest(
            source_path=str(source),
            alias="demo-main",
            commit_message="  Initial local import  ",
        )
    )

    uuid.UUID(result.skill_id)
    uuid.UUID(result.version_id)
    assert result.active_version_id == result.version_id
    manifest = repository.get_manifest("demo-main")
    metadata = repository.get_version(result.skill_id, result.version_id)
    snapshot = repository.read_snapshot(result.skill_id)
    assert manifest.active_version_id == result.version_id
    assert manifest.version_ids == [result.version_id]
    assert metadata.parent_version_id is None
    assert metadata.commit_message == "Initial local import"
    assert metadata.sync_state == LocalSkillSyncState.PENDING
    assert snapshot.file_contents["references/api.md"] == "private reference\n"
    assert not hasattr(manifest, "path")
    assert (repository.root / "skills" / result.skill_id / "manifest.json").is_file()
    files_payload = json.loads(
        (
            repository.root
            / "skills"
            / result.skill_id
            / "versions"
            / result.version_id
            / "files.json"
        ).read_text(encoding="utf-8")
    )
    assert [entry["path"] for entry in files_payload["algorithm_files"]] == [
        "SKILL.md"
    ]
    assert [entry["path"] for entry in files_payload["linked_files"]] == [
        "references/api.md",
        "scripts/check.py",
    ]
    assert "files" not in files_payload
    assert (repository.root / "blobs" / "bundles" / metadata.content_hash / "content").is_file()
    assert len(repository.load_outbox().operations) == 1
    assert repository.load_outbox().operations[0].version_id == result.version_id


def test_duplicate_registration_requires_explicit_choice_and_can_reuse(tmp_path):
    repository = _repository(tmp_path)
    source = _source(tmp_path)
    first = repository.register(RegisterLocalRequest(source_path=str(source)))

    with pytest.raises(LocalSkillRepositoryError, match="duplicate_action"):
        repository.register(RegisterLocalRequest(source_path=str(source)))

    reused = repository.register(
        RegisterLocalRequest(
            source_path=str(source),
            duplicate_action=DuplicateSkillAction.REUSE,
        )
    )
    created = repository.register(
        RegisterLocalRequest(
            source_path=str(source),
            duplicate_action=DuplicateSkillAction.CREATE_NEW,
        )
    )

    assert reused.action == "reused"
    assert reused.skill_id == first.skill_id
    assert reused.version_id == first.version_id
    assert reused.summary is not None
    assert created.action == "created"
    assert created.skill_id != first.skill_id
    assert created.version_id != first.version_id


def test_publish_is_immutable_and_switch_only_changes_active_pointer(tmp_path):
    repository = _repository(tmp_path)
    source = _source(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(source)))
    original_metadata_path = (
        repository.root
        / "skills"
        / registered.skill_id
        / "versions"
        / registered.version_id
        / "metadata.json"
    )
    original_metadata = original_metadata_path.read_bytes()

    published = repository.publish(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            content='name: demo\nversion: "1.1.0"\n\nEdited\n',
            commit_message="Editor change",
        )
    )

    assert published.version_id != registered.version_id
    assert published.active_version_id == registered.version_id
    new_metadata = repository.get_version(registered.skill_id, published.version_id)
    assert new_metadata.parent_version_id == registered.version_id
    assert repository.read_snapshot(registered.skill_id, published.version_id).linked_files == {
        "references/api.md": "private reference\n",
        "scripts/check.py": "print('ok')\n",
    }
    assert original_metadata_path.read_bytes() == original_metadata

    switched = repository.switch(registered.skill_id, published.version_id)

    assert switched.active_version_id == published.version_id
    assert repository.list_versions(registered.skill_id) == [
        repository.get_version(registered.skill_id, registered.version_id),
        new_metadata,
    ]
    assert original_metadata_path.read_bytes() == original_metadata
    assert len(repository.load_outbox().operations) == 2


def test_export_restores_complete_selected_version_and_replaces_stale_target(tmp_path):
    repository = _repository(tmp_path)
    source = _source(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(source)))
    (source / "SKILL.md").write_text('name: demo\nversion: "2.0.0"\n\nSecond\n', encoding="utf-8")
    (source / "references" / "api.md").unlink()
    (source / "references" / "new.md").write_text("new reference\n", encoding="utf-8")
    published = repository.publish(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            source_path=str(source),
            activate=True,
        )
    )
    target = tmp_path / "exported"
    target.mkdir()
    (target / "stale.txt").write_text("stale", encoding="utf-8")

    exported = repository.export(
        ExportSkillRequest(
            skill_id=registered.skill_id,
            target_path=str(target),
        )
    )

    assert exported.version_id == published.version_id
    assert (target / "SKILL.md").read_text(encoding="utf-8").endswith("Second\n")
    assert (target / "references" / "new.md").read_text(encoding="utf-8") == "new reference\n"
    assert not (target / "references" / "api.md").exists()
    assert not (target / "stale.txt").exists()
    assert (target / "scripts" / "check.py").stat().st_mode & 0o777 == 0o755
    assert repository.get_manifest(registered.skill_id).active_version_id == published.version_id

    old_target = tmp_path / "old-export"
    old_export = repository.export(
        ExportSkillRequest(
            skill_id=registered.skill_id,
            version_id=registered.version_id,
            target_path=str(old_target),
        )
    )
    assert old_export.version_id == registered.version_id
    assert (old_target / "references" / "api.md").exists()
    assert repository.get_manifest(registered.skill_id).active_version_id == published.version_id


def test_binary_linked_file_is_rejected_without_partial_repository_state(tmp_path):
    repository = _repository(tmp_path)
    source = _source(tmp_path)
    (source / "asset.bin").write_bytes(b"\xff\x00")

    with pytest.raises(SkillSnapshotError, match="binary linked file"):
        repository.register(RegisterLocalRequest(source_path=str(source)))

    assert repository.list_manifests() == []
    assert not repository.outbox_path.exists()


def test_export_rejects_repository_overlap_and_non_directory_target(tmp_path):
    repository = _repository(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))

    with pytest.raises(LocalSkillRepositoryError, match="overlaps"):
        repository.export(
            ExportSkillRequest(
                skill_id=registered.skill_id,
                target_path=str(repository.root / "exported"),
            )
        )

    file_target = tmp_path / "file-target"
    file_target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(LocalSkillRepositoryError, match="not a directory"):
        repository.export(
            ExportSkillRequest(
                skill_id=registered.skill_id,
                target_path=str(file_target),
            )
        )


def test_skill_manager_exposes_one_application_facade_for_local_repository(tmp_path):
    source = _source(tmp_path)
    config_manager = ConfigManager(config_dir=tmp_path / "config")

    class _UnusedCloud:
        pass

    manager = SkillManager.from_config_manager(config_manager, _UnusedCloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(source), alias="managed"))
    published = manager.publish_local(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            content='name: demo\nversion: "1.1.0"\n\nManaged editor version\n',
        )
    )

    assert manager.show_local("managed").active_version_id == registered.version_id
    assert [version.version_id for version in manager.local_history("managed")] == [
        registered.version_id,
        published.version_id,
    ]
    assert manager.rollback_local("managed", version_id=published.version_id).active_version_id == published.version_id
    assert manager.get_local_snapshot("managed").file_contents["SKILL.md"].endswith("Managed editor version\n")
    context = manager.ensure_skill_context(registered.skill_id, usage="injected")
    assert context.base_version_id == published.version_id
    assert context.content_hash == manager.local_history("managed")[-1].content_hash
    assert manager.skill_id_for_context(context) == registered.skill_id


def test_cloud_import_inherits_private_files_without_changing_active_pointer(tmp_path):
    repository = _repository(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    root = repository.get_version(registered.skill_id, registered.version_id)
    repository.mark_version_synced(
        registered.skill_id,
        version_id=registered.version_id,
        cloud_skill_id="00000000-0000-4000-8000-000000000010",
        cloud_status=SkillVersionStatus.OBSERVED,
    )
    cloud_content = '[{"content":"name: demo\\nversion: \\"2.0.0\\"\\n\\nCloud draft\\n","path":"SKILL.md"}]'
    from mindmemos_sdk.skills.bundle import bundle_files_from_content, compute_content_hash

    summary = PullVersionSummary(
        version_id="00000000-0000-4000-8000-000000000020",
        parent_version_id=registered.version_id,
        content_hash=compute_content_hash(bundle_files_from_content(cloud_content)),
        version_label="2.0.0",
        commit_message="Cloud evolved draft",
        origin="cloud",
        status="draft",
        created_at="2026-07-25T00:00:00Z",
        received_at="2026-07-25T00:00:01Z",
    )

    imported = repository.import_cloud_version(
        registered.skill_id,
        summary=summary,
        content=cloud_content,
    )

    manifest = repository.get_manifest(registered.skill_id)
    snapshot = repository.read_snapshot(registered.skill_id, imported.version_id)
    assert manifest.active_version_id == registered.version_id
    assert manifest.published_head_id is None
    assert imported.parent_version_id == root.version_id
    assert imported.sync_state == LocalSkillSyncState.SYNCED
    assert snapshot.file_contents["SKILL.md"].endswith("Cloud draft\n")
    assert snapshot.linked_files == repository.read_snapshot(registered.skill_id, root.version_id).linked_files

    completed = repository.complete_sync(
        registered.skill_id,
        published_head_id=imported.version_id,
        cloud_revision=4,
        synced_at="2026-07-25T00:01:00Z",
    )
    assert completed.published_head_id == imported.version_id
    assert completed.active_version_id == registered.version_id
    assert completed.last_sync_at == "2026-07-25T00:01:00Z"


def test_manager_push_uses_shared_uuid_excludes_private_files_and_clears_outbox(tmp_path):
    source = _source(tmp_path)
    config_manager = ConfigManager(config_dir=tmp_path / "config")
    captured = {}

    class _Cloud:
        def push_version(self, request):
            captured["request"] = request
            return PushVersionResult(
                cloud_skill_id="00000000-0000-4000-8000-000000000010",
                version_id=request.version_id,
                content_hash=request.expected_content_hash,
                status="observed",
                created_at=request.created_at,
                received_at="2026-07-25T00:00:01Z",
            )

    manager = SkillManager.from_config_manager(config_manager, _Cloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(source)))
    active_before = manager.show_local(registered.skill_id).active_version_id

    result = manager.push_local(registered.skill_id)

    request = captured["request"]
    assert result.version_id == registered.version_id
    assert request.version_id == registered.version_id
    assert request.model_dump().keys().isdisjoint(
        {"linked_files", "local_snapshot_hash", "active_version_id", "source_path", "path"}
    )
    assert "private reference" not in request.content
    manifest = manager.show_local(registered.skill_id)
    assert manifest.cloud_skill_id == result.cloud_skill_id
    assert manifest.active_version_id == active_before
    assert manifest.published_head_id is None
    assert manager.local_repository.load_outbox().operations == []
    assert manager.local_history(registered.skill_id)[0].sync_state == LocalSkillSyncState.SYNCED


def test_manager_push_failure_keeps_failed_outbox_for_retry(tmp_path):
    config_manager = ConfigManager(config_dir=tmp_path / "config")

    class _Cloud:
        def push_version(self, request):
            raise RuntimeError("offline")

    manager = SkillManager.from_config_manager(config_manager, _Cloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(_source(tmp_path))))

    with pytest.raises(SkillRegistryError, match="failed to push"):
        manager.push_local(registered.skill_id)

    [operation] = manager.local_repository.load_outbox().operations
    assert operation.status.value == "failed"
    assert operation.attempt_count == 1
    assert operation.last_error_code == "RuntimeError"


def test_manager_sync_imports_cloud_child_then_updates_published_pointer_only(tmp_path):
    config_manager = ConfigManager(config_dir=tmp_path / "config")
    cloud_skill_id = "00000000-0000-4000-8000-000000000010"
    child_id = "00000000-0000-4000-8000-000000000020"
    cloud_content = (
        '[{"content":"name: demo\\nversion: \\"2.0.0\\"\\n\\nCloud draft\\n",'
        '"path":"SKILL.md"}]'
    )
    from mindmemos_sdk.skills.bundle import bundle_files_from_content, compute_content_hash

    child = PullVersionSummary(
        version_id=child_id,
        parent_version_id=None,
        content_hash=compute_content_hash(bundle_files_from_content(cloud_content)),
        version_label="2.0.0",
        commit_message="Cloud draft",
        origin="cloud",
        status="ready",
        created_at="2026-07-25T00:00:00Z",
        received_at="2026-07-25T00:00:01Z",
    )

    class _Cloud:
        def __init__(self):
            self.root_version_id = None

        def push_version(self, request):
            self.root_version_id = request.version_id
            return PushVersionResult(
                cloud_skill_id=cloud_skill_id,
                version_id=request.version_id,
                content_hash=request.expected_content_hash,
                status="observed",
                created_at=request.created_at,
                received_at="2026-07-25T00:00:01Z",
            )

        def sync_cloud(self, request):
            assert request.items[0].known_version_ids == [self.root_version_id]
            return SyncCloudResult(
                items=[
                    SyncCloudResultItem(
                        cloud_skill_id=cloud_skill_id,
                        versions=[child.model_copy(update={"parent_version_id": self.root_version_id})],
                        published_head_id=child_id,
                        cloud_revision=7,
                    )
                ]
            )

        def pull_content(self, requested_skill_id, version_id):
            assert requested_skill_id == cloud_skill_id
            assert version_id == child_id
            return PullVersionContent(
                version=child.model_copy(update={"parent_version_id": self.root_version_id}),
                content=cloud_content,
            )

    cloud = _Cloud()
    manager = SkillManager.from_config_manager(config_manager, cloud)
    registered = manager.register_local(RegisterLocalRequest(source_path=str(_source(tmp_path))))

    synced = manager.sync_local(registered.skill_id)

    assert synced.active_version_id == registered.version_id
    assert synced.published_head_id == child_id
    assert synced.cloud_revision == 7
    assert synced.last_sync_at is not None
    assert manager.get_local_snapshot(registered.skill_id, version_id=child_id).linked_files == {
        "references/api.md": "private reference\n",
        "scripts/check.py": "print('ok')\n",
    }


def test_manager_promote_changes_only_cached_cloud_pointer(tmp_path):
    config_manager = ConfigManager(config_dir=tmp_path / "config")
    cloud_skill_id = "00000000-0000-4000-8000-000000000010"

    class _Cloud:
        def push_version(self, request):
            return PushVersionResult(
                cloud_skill_id=cloud_skill_id,
                version_id=request.version_id,
                content_hash=request.expected_content_hash,
                status="observed",
                created_at=request.created_at,
                received_at="2026-07-25T00:00:01Z",
            )

        def sync_cloud(self, request):
            return SyncCloudResult(
                items=[
                    SyncCloudResultItem(
                        cloud_skill_id=cloud_skill_id,
                        versions=[],
                        published_head_id=None,
                        cloud_revision=2,
                    )
                ]
            )

        def promote(self, requested_skill_id, request):
            assert requested_skill_id == cloud_skill_id
            return PromoteCloudResult(
                cloud_skill_id=cloud_skill_id,
                published_head_id=request.version_id,
                cloud_revision=3,
                updated_at="2026-07-25T00:02:00Z",
            )

    manager = SkillManager.from_config_manager(config_manager, _Cloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    manager.sync_local(registered.skill_id)

    result = manager.promote_local(registered.skill_id, version_id=registered.version_id)

    manifest = manager.show_local(registered.skill_id)
    assert result.published_head_id == registered.version_id
    assert manifest.published_head_id == registered.version_id
    assert manifest.cloud_revision == 3
    assert manifest.active_version_id == registered.version_id
    assert manager.local_repository.load_outbox().operations == []


def test_manager_promote_failure_keeps_idempotent_outbox_operation(tmp_path):
    config_manager = ConfigManager(config_dir=tmp_path / "config")
    cloud_skill_id = "00000000-0000-4000-8000-000000000010"

    class _Cloud:
        def promote(self, requested_skill_id, request):
            raise RuntimeError("offline")

    manager = SkillManager.from_config_manager(config_manager, _Cloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    manager.local_repository.mark_version_synced(
        registered.skill_id,
        version_id=registered.version_id,
        cloud_skill_id=cloud_skill_id,
        cloud_status=SkillVersionStatus.OBSERVED,
    )
    manager.local_repository.remove_outbox_operation(
        manager.local_repository.load_outbox().operations[0].operation_id
    )
    manager.local_repository.complete_sync(
        registered.skill_id,
        published_head_id=None,
        cloud_revision=2,
    )

    with pytest.raises(SkillRegistryError, match="failed to promote"):
        manager.promote_local(
            registered.skill_id,
            version_id=registered.version_id,
        )

    [operation] = manager.local_repository.load_outbox().operations
    assert operation.operation_type.value == "promote"
    assert operation.version_id == registered.version_id
    assert operation.expected_cloud_revision == 2
    assert operation.status.value == "failed"
    assert operation.attempt_count == 1
    assert operation.next_retry_at is not None


def test_cloud_conflict_marks_existing_version_without_mutating_immutable_fields(tmp_path):
    repository = _repository(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    original = repository.get_version(registered.skill_id, registered.version_id)
    summary = PullVersionSummary(
        version_id=original.version_id,
        parent_version_id=original.parent_version_id,
        content_hash=original.content_hash,
        version_label=original.version_label,
        commit_message="different immutable message",
        origin=original.origin,
        status="ready",
        created_at=original.created_at,
        received_at="2026-07-25T00:00:01Z",
    )

    with pytest.raises(LocalSkillRepositoryError, match="conflicts"):
        repository.import_cloud_version(
            registered.skill_id,
            summary=summary,
            content=repository.read_snapshot(
                registered.skill_id,
                registered.version_id,
            ).content,
        )

    conflicted = repository.get_version(registered.skill_id, registered.version_id)
    assert conflicted.sync_state == LocalSkillSyncState.CONFLICT
    assert conflicted.commit_message == original.commit_message


def test_pull_order_is_parent_first_and_rejects_incomplete_graph(tmp_path):
    repository = _repository(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    manifest = repository.get_manifest(registered.skill_id)

    child = PullVersionSummary(
        version_id="00000000-0000-4000-8000-000000000020",
        parent_version_id=registered.version_id,
        content_hash="b" * 64,
        origin="cloud",
        status="draft",
        created_at="2026-07-25T00:01:00Z",
        received_at="2026-07-25T00:01:01Z",
    )
    grandchild = child.model_copy(
        update={
            "version_id": "00000000-0000-4000-8000-000000000030",
            "parent_version_id": child.version_id,
            "content_hash": "c" * 64,
            "received_at": "2026-07-25T00:02:01Z",
        }
    )

    ordered = SkillManager._order_pull_summaries(
        manifest,
        [grandchild, child],
    )

    assert [item.version_id for item in ordered] == [
        child.version_id,
        grandchild.version_id,
    ]

    orphan = child.model_copy(
        update={
            "version_id": "00000000-0000-4000-8000-000000000040",
            "parent_version_id": "00000000-0000-4000-8000-000000000099",
        }
    )
    with pytest.raises(SkillRegistryError, match="missing parents or a cycle"):
        SkillManager._order_pull_summaries(manifest, [orphan])


def test_outbox_recovery_rebuilds_missing_push_and_releases_expired_lease(tmp_path):
    repository = _repository(tmp_path)
    registered = repository.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    repository.outbox_path.unlink()

    rebuilt = repository.recover_outbox()

    assert len(rebuilt.operations) == 1
    operation = rebuilt.operations[0]
    assert operation.skill_id == registered.skill_id
    assert operation.version_id == registered.version_id
    assert operation.status.value == "pending"

    running = repository.mark_outbox_running(operation.operation_id)
    assert running.status.value == "running"
    assert running.attempt_count == 1

    recovered = repository.recover_outbox(lease_seconds=0)

    assert recovered.operations[0].status.value == "pending"
    assert recovered.operations[0].attempt_count == 1
