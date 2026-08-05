"""Phase 0 contracts for moving Skill management behind SkillApplication.

These tests intentionally exercise only the public ``SkillManager`` facade.  In
Phase 6 the fixture can be backed by the SDK adapter for ``SkillApplication``
without changing the behavior assertions below.
"""

from __future__ import annotations

from pathlib import Path

from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.skills import (
    DuplicateSkillAction,
    ExportSkillRequest,
    PromoteCloudResult,
    PublishLocalRequest,
    PushVersionResult,
    RegisterLocalRequest,
    SkillManager,
    SyncCloudResult,
    SyncCloudResultItem,
)
from mindmemos_sdk.ui.skill_service import (
    SkillCompareView,
    SkillContentView,
    SkillDetailView,
    SkillListItemView,
    SkillVersionView,
)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        'name: demo\nversion: "1.0.0"\n\nOriginal body\n',
        encoding="utf-8",
    )
    references = source / "references"
    references.mkdir()
    (references / "guide.md").write_text("Private guide\n", encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    script.chmod(0o755)
    return source


def _manager(tmp_path: Path, cloud: object | None = None) -> SkillManager:
    return SkillManager.from_config_manager(
        ConfigManager(config_dir=tmp_path / "config"),
        cloud or object(),
    )


def test_local_management_contract_survives_application_migration(tmp_path: Path) -> None:
    """Pin the local use-case behavior that the future Application must own."""

    source = _source(tmp_path)
    manager = _manager(tmp_path)
    registered = manager.register_local(
        RegisterLocalRequest(
            source_path=str(source),
            alias="demo-main",
            version_label="1.0.0",
            commit_message="Initial import",
        )
    )

    assert registered.action == "created"
    assert registered.active_version_id == registered.version_id
    assert manager.list_local() == [manager.show_local("demo-main")]

    reused = manager.register_local(
        RegisterLocalRequest(
            source_path=str(source),
            duplicate_action=DuplicateSkillAction.REUSE,
        )
    )
    assert (reused.action, reused.skill_id, reused.version_id) == (
        "reused",
        registered.skill_id,
        registered.version_id,
    )

    published = manager.publish_local(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            content='name: demo\nversion: "1.1.0"\n\nEdited body\n',
            version_label="1.1.0",
            commit_message="Editor change",
        )
    )
    assert published.version_id != registered.version_id
    assert published.active_version_id == registered.version_id
    assert [version.version_id for version in manager.local_history("demo-main")] == [
        registered.version_id,
        published.version_id,
    ]

    difference = manager.diff_local(
        "demo-main",
        from_version_id=registered.version_id,
        to_version_id=published.version_id,
    )
    assert "-Original body" in difference.diff
    assert "+Edited body" in difference.diff

    switched = manager.switch_local("demo-main", version_id=published.version_id)
    assert switched.active_version_id == published.version_id
    rolled_back = manager.rollback_local("demo-main", version_id=registered.version_id)
    assert rolled_back.active_version_id == registered.version_id

    target = tmp_path / "exported"
    exported = manager.export_local(
        ExportSkillRequest(
            skill_id=registered.skill_id,
            version_id=published.version_id,
            target_path=str(target),
        )
    )
    assert exported.version_id == published.version_id
    assert exported.exported_files == [
        "SKILL.md",
        "references/guide.md",
        "scripts/check.py",
    ]
    assert (target / "SKILL.md").read_text(encoding="utf-8").endswith("Edited body\n")
    assert (target / "references" / "guide.md").read_text(encoding="utf-8") == "Private guide\n"
    assert manager.show_local("demo-main").active_version_id == registered.version_id


def test_sync_and_promote_never_implicitly_change_effective_version(tmp_path: Path) -> None:
    """Keep the local effective pointer independent from the cloud published head."""

    cloud_skill_id = "00000000-0000-4000-8000-000000000010"

    class Cloud:
        def push_version(self, request):
            return PushVersionResult(
                cloud_skill_id=cloud_skill_id,
                version_id=request.version_id,
                content_hash=request.expected_content_hash,
                status="observed",
                created_at=request.created_at,
                received_at="2026-08-05T00:00:00Z",
            )

        def sync_cloud(self, request):
            assert request.items[0].cloud_skill_id == cloud_skill_id
            return SyncCloudResult(
                items=[
                    SyncCloudResultItem(
                        cloud_skill_id=cloud_skill_id,
                        versions=[],
                        published_head_id=None,
                        cloud_revision=1,
                    )
                ]
            )

        def promote(self, requested_skill_id, request):
            assert requested_skill_id == cloud_skill_id
            return PromoteCloudResult(
                cloud_skill_id=cloud_skill_id,
                published_head_id=request.version_id,
                cloud_revision=2,
                updated_at="2026-08-05T00:01:00Z",
            )

    manager = _manager(tmp_path, Cloud())
    registered = manager.register_local(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    published = manager.publish_local(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            content='name: demo\nversion: "1.1.0"\n\nCandidate\n',
            version_label="1.1.0",
        )
    )

    synced = manager.sync_local(registered.skill_id)
    assert synced.active_version_id == registered.version_id
    assert synced.published_head_id is None

    promoted = manager.promote_local(
        registered.skill_id,
        version_id=published.version_id,
    )
    current = manager.show_local(registered.skill_id)
    assert promoted.published_head_id == published.version_id
    assert current.published_head_id == published.version_id
    assert current.active_version_id == registered.version_id


def test_sdk_ui_dto_field_contract_is_explicit() -> None:
    """Freeze fields consumed by the current UI while its data source moves."""

    assert set(SkillListItemView.model_fields) == {
        "skill_id",
        "name",
        "alias",
        "cloud_skill_id",
        "active_version_id",
        "published_head_id",
        "cloud_revision",
        "version_count",
        "pending_count",
        "sync_state",
        "last_sync_at",
    }
    assert set(SkillVersionView.model_fields) == {
        "version_id",
        "parent_version_id",
        "version_label",
        "commit_message",
        "content_hash",
        "local_snapshot_hash",
        "origin",
        "status",
        "is_active",
        "is_published",
        "has_linked_files",
        "sync_state",
        "created_at",
    }
    assert set(SkillDetailView.model_fields) == {
        "skill",
        "versions",
        "active_version",
        "published_version",
        "outbox_operations",
    }
    assert set(SkillContentView.model_fields) == {"skill_id", "version_id", "content"}
    assert set(SkillCompareView.model_fields) == {
        "from_version_id",
        "to_version_id",
        "content_diff",
        "linked_file_changes",
    }
