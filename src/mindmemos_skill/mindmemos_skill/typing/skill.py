"""Algorithm-facing Skill definition, version and binding aggregates."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class SkillUsageType(StrEnum):
    """How a skill was used in one agent trajectory."""

    INJECTED = "injected"
    """技能注入供Agent使用"""

    MODIFIED = "modified"
    """技能修改使用"""

    UNUSED = "unused"
    """技能注入但未使用"""


class SkillVersionStatus(StrEnum):
    """Lifecycle state of one immutable Skill version."""

    DRAFT = "draft"
    REJECTED = "rejected"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class SkillVersionOrigin(StrEnum):
    """Source that created one immutable Skill version."""

    LOCAL = "local"
    CLOUD = "cloud"
    EVOLUTION = "evolution"
    MANAGE = "manage"


class Skill(BaseModel):
    """Definition of one skill available to an agent.

    A skill contains the material the backend can inject or expose to the
    agent.  Version and trace identity do not belong here; they are captured
    by :class:`SkillBinding` when a trajectory uses this skill.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    """技能名称"""

    description: str | None = None
    """技能描述"""

    alias: str | None = None
    """供 CLI 或算法检索使用的可选短名称。"""

    content: str = Field(min_length=1)
    """技能正文"""

    linked_files: dict[str, str] = Field(default_factory=dict)
    """技能相关的文件和内容，包含scripts, references等，key为文件相对路径，value为文件内容"""

    resources: dict[str, str] = Field(default_factory=dict)
    """不属于核心 Skill bundle、但算法执行时需要的辅助文本资源。"""

    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """skill完整meta信息"""

    @model_validator(mode="after")
    def validate_files(self) -> Skill:
        invalid_paths = [path for path in (*self.linked_files, *self.resources) if not path]
        if invalid_paths:
            raise ValueError("Skill file paths must not be empty")
        duplicate_paths = self.linked_files.keys() & self.resources.keys()
        if duplicate_paths:
            duplicates = ", ".join(sorted(duplicate_paths))
            raise ValueError(f"Skill linked files and resources overlap: {duplicates}")
        return self

    def bundle_files(self) -> dict[str, str]:
        """Return the core multi-file bundle stored by ``SkillRecord.blob``."""

        return {"SKILL.md": self.content, **self.linked_files}


class SkillVersion(BaseModel):
    """One immutable Skill version with its executable definition nested.

    ``SkillRecord`` persists this aggregate as a flat row. Algorithms receive
    the nested form so content, lineage and lifecycle are not mixed together.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    skill_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    cloud_skill_id: str | None = None
    parent_version_ids: list[str] = Field(default_factory=list)
    version_label: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    content_hash: str = Field(min_length=1)
    status: SkillVersionStatus = SkillVersionStatus.DRAFT
    origin: SkillVersionOrigin = SkillVersionOrigin.LOCAL
    skill: Skill
    commit_message: str | None = None
    created_at: datetime
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parent_ids(self) -> SkillVersion:
        if self.version_id in self.parent_version_ids:
            raise ValueError("a Skill version cannot be its own parent")
        if len(self.parent_version_ids) != len(set(self.parent_version_ids)):
            raise ValueError("parent_version_ids may not contain duplicates")
        return self


class SkillBinding(BaseModel):
    """Reference to one skill used by a single execution trajectory.

    The fields mirror the SDK/server skill-reference contract.  ``version_id``
    can be absent while the content is still pending registration; this is
    the same unresolved-binding state used by the skill trace flow.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    """技能名称"""

    content_hash: str = Field(min_length=1)
    """技能内容哈希"""

    skill_id: str | None = None
    """已注册时指向本地 Skill 家族；未注册时为空。"""

    base_version_id: str | None = None
    """当前 Skill 内容派生自的版本；根版本或未知时为空。"""

    version_id: str | None = None
    """本次轨迹实际使用的不可变版本；尚未注册或绑定时为空。"""

    version_label: str | None = None
    """供算法报告展示的版本标签。"""

    usage: SkillUsageType | None = None
    """技能使用方式"""


__all__ = [
    "Skill",
    "SkillBinding",
    "SkillUsageType",
    "SkillVersion",
    "SkillVersionOrigin",
    "SkillVersionStatus",
]
