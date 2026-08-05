"""Standalone local Skill management API."""

from .bundle import compute_content_hash, deserialize_files, parse_version_label, serialize_files
from .detector import detect_openclaw_skill_candidates
from .installer import SkillInstaller
from .models import (
    DetectedSkillCandidate,
    DetectedSkillUsage,
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
    SnapshotFile,
    SnapshotFileRole,
)
from .repository import SkillRepository
from .service import LocalSkillManager
from .snapshot import read_skill_snapshot, snapshot_from_editor, snapshot_from_record

__all__ = [
    "DuplicateAction",
    "DetectedSkillCandidate",
    "DetectedSkillUsage",
    "ExportSkillRequest",
    "ExportSkillResult",
    "LocalSkillManager",
    "ManagedSkill",
    "PublishSkillRequest",
    "PublishSkillResult",
    "RegisterSkillRequest",
    "RegisterSkillResult",
    "SkillDetail",
    "SkillDiffResult",
    "SkillInstaller",
    "SkillRepository",
    "SkillSnapshot",
    "SnapshotFile",
    "SnapshotFileRole",
    "compute_content_hash",
    "detect_openclaw_skill_candidates",
    "deserialize_files",
    "parse_version_label",
    "read_skill_snapshot",
    "serialize_files",
    "snapshot_from_editor",
    "snapshot_from_record",
]
