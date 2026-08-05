"""Public data contracts for standalone local Skill management."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import SkillFamilyStateRecord, SkillRecord


class SnapshotFileRole(StrEnum):
    ALGORITHM = "algorithm"
    SCRIPT = "script"
    REFERENCE = "reference"
    RESOURCE = "resource"


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content_hash: str
    byte_size: int = Field(ge=0)
    mode: int | None = None
    media_type: str | None = None
    role: SnapshotFileRole


class SkillSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blob: dict[str, str]
    resources: dict[str, str] = Field(default_factory=dict)
    files: list[SnapshotFile]
    content_hash: str
    local_snapshot_hash: str

    @property
    def file_contents(self) -> dict[str, str]:
        return {**self.blob, **self.resources}


class DuplicateAction(StrEnum):
    REUSE = "reuse"
    CREATE_NEW = "create_new"


class DetectedSkillUsage(StrEnum):
    INJECTED = "injected"
    MODIFIED = "modified"


class DetectedSkillCandidate(BaseModel):
    """Agent-family-specific evidence found in a message trajectory."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    name: str
    version_label: str | None = None
    usage: DetectedSkillUsage


class RegisterSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    source_path: str | Path
    name: str | None = None
    alias: str | None = None
    version_label: str | None = None
    commit_message: str | None = None
    duplicate_action: DuplicateAction | None = None


class RegisterSkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "reused"]
    skill_id: str
    version_id: str
    effective_version_id: str


class PublishSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    skill_ref: str
    base_version_id: str | None = None
    source_path: str | Path | None = None
    content: str | None = None
    version_label: str | None = None
    commit_message: str | None = None
    activate: bool = False


class PublishSkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version_id: str
    effective_version_id: str
    local_snapshot_hash: str


class ManagedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str
    alias: str | None = None
    cloud_skill_id: str | None = None
    effective_version_id: str
    published_head_id: str | None = None
    cloud_revision: int | None = None
    last_sync_at: datetime | None = None
    version_count: int
    pending_count: int
    created_at: datetime
    updated_at: datetime


class SkillDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: ManagedSkill
    effective_version: SkillRecord
    state: SkillFamilyStateRecord


class ExportSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    skill_ref: str
    target_path: str | Path
    version_id: str | None = None
    replace: bool = True


class ExportSkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version_id: str
    target_path: str
    exported_files: list[str]
    local_snapshot_hash: str


class SkillDiffResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    from_version_id: str
    to_version_id: str
    diff: str
    changed_files: list[str]


__all__ = [
    "DetectedSkillCandidate",
    "DetectedSkillUsage",
    "DuplicateAction",
    "ExportSkillRequest",
    "ExportSkillResult",
    "ManagedSkill",
    "PublishSkillRequest",
    "PublishSkillResult",
    "RegisterSkillRequest",
    "RegisterSkillResult",
    "SkillDetail",
    "SkillDiffResult",
    "SkillSnapshot",
    "SnapshotFile",
    "SnapshotFileRole",
]
