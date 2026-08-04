"""Agent skill and trajectory-binding contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillUsageType(str, Enum):
    """How a skill was used in one agent trajectory."""

    INJECTED = "injected"
    """技能注入使用"""

    MODIFIED = "modified"
    """技能修改使用"""

    UNUSED = "unused"
    """技能注入但未使用"""


class Skill(BaseModel):
    """Definition of one skill available to an agent.

    A skill contains the material the backend can inject or expose to the
    agent.  Version and trace identity do not belong here; they are captured
    by :class:`SkillBinding` when a trajectory uses this skill.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    """技能名称"""

    description: str
    """技能描述"""

    content: str
    """技能正文"""

    linked_files: dict[str, str] = Field(default_factory=dict)
    """技能相关的文件和内容，包含scripts, references等，key为文件相对路径，value为文件内容"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """skill完整meta信息"""


class SkillBinding(BaseModel):
    """Reference to one skill used by a single execution trajectory.

    The fields mirror the SDK/server skill-reference contract.  ``version_id``
    can be absent while the content is still pending registration; this is
    the same unresolved-binding state used by the skill trace flow.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    """技能名称"""

    content_hash: str
    """技能内容哈希"""

    usage: SkillUsageType | None = None
    """技能使用方式"""


__all__ = ["Skill", "SkillBinding", "SkillUsageType"]
