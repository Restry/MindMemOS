"""Tests for the local UI Skill application service."""

from __future__ import annotations

from pathlib import Path

from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.skills import (
    ExportSkillRequest,
    LocalSkillSyncState,
    PublishLocalRequest,
    RegisterLocalRequest,
    SkillManager,
)
from mindmemos_sdk.ui import LocalSkillUIService


class _UnusedCloud:
    pass


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text('name: demo\nversion: "1.0.0"\n\nBody\n', encoding="utf-8")
    (source / "references").mkdir()
    (source / "references" / "private.md").write_text("private\n", encoding="utf-8")
    return source


def test_ui_service_uses_immutable_versions_and_shared_manager(tmp_path):
    manager = SkillManager.from_config_manager(
        ConfigManager(config_dir=tmp_path / "config"),
        _UnusedCloud(),
    )
    service = LocalSkillUIService(manager)
    registered = service.register(RegisterLocalRequest(source_path=str(_source(tmp_path)), alias="demo-main"))

    items = service.list_skills()
    detail = service.detail("demo-main")
    content = service.content("demo-main")

    assert len(items) == 1
    assert items[0].skill_id == registered.skill_id
    assert items[0].pending_count == 1
    assert items[0].sync_state == "pending"
    assert items[0].cloud_revision is None
    assert items[0].model_dump().get("path") is None
    assert detail.skill.active_version_id == registered.version_id
    assert detail.active_version.is_active is True
    assert detail.active_version.has_linked_files is True
    assert content.version_id == registered.version_id
    assert content.content.endswith("Body\n")

    published, unchanged_detail = service.publish(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            base_version_id=registered.version_id,
            content='name: demo\nversion: "1.1.0"\n\nEdited\n',
            commit_message="UI draft",
        )
    )

    assert published.active_version_id == registered.version_id
    assert unchanged_detail.active_version.version_id == registered.version_id
    assert unchanged_detail.versions[-1].commit_message == "UI draft"
    assert unchanged_detail.versions[-1].is_active is False

    switched = service.switch("demo-main", published.version_id)
    assert switched.active_version.version_id == published.version_id
    assert service.content("demo-main").content.endswith("Edited\n")


def test_ui_service_compare_and_export_include_local_linked_file_changes(tmp_path):
    source = _source(tmp_path)
    manager = SkillManager.from_config_manager(
        ConfigManager(config_dir=tmp_path / "config"),
        _UnusedCloud(),
    )
    service = LocalSkillUIService(manager)
    registered = service.register(RegisterLocalRequest(source_path=str(source)))
    (source / "SKILL.md").write_text('name: demo\nversion: "2.0.0"\n\nSecond\n', encoding="utf-8")
    (source / "references" / "private.md").write_text("changed private\n", encoding="utf-8")
    published, _detail = service.publish(
        PublishLocalRequest(
            skill_id=registered.skill_id,
            source_path=str(source),
            activate=True,
        )
    )

    comparison = service.compare(registered.skill_id, registered.version_id, published.version_id)
    exported = service.export(
        ExportSkillRequest(
            skill_id=registered.skill_id,
            target_path=str(tmp_path / "export"),
        )
    )

    assert "+Second" in comparison.content_diff
    assert comparison.linked_file_changes == ["references/private.md"]
    assert exported.version_id == published.version_id
    assert (tmp_path / "export" / "references" / "private.md").read_text(encoding="utf-8") == "changed private\n"


def test_ui_list_surfaces_version_conflict_over_pending_summary(tmp_path):
    manager = SkillManager.from_config_manager(
        ConfigManager(config_dir=tmp_path / "config"),
        _UnusedCloud(),
    )
    service = LocalSkillUIService(manager)
    registered = service.register(RegisterLocalRequest(source_path=str(_source(tmp_path))))
    metadata = manager.local_repository.get_version(
        registered.skill_id,
        registered.version_id,
    )
    manager.local_repository._atomic_write_model(
        manager.local_repository._metadata_path(
            registered.skill_id,
            registered.version_id,
        ),
        metadata.model_copy(update={"sync_state": LocalSkillSyncState.CONFLICT}),
    )

    [item] = service.list_skills()

    assert item.sync_state == "conflict"
