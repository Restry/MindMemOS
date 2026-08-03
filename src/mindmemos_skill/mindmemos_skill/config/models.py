"""Configuration contracts consumed by the local Skill composition root.

The package deliberately stores model *references* and algorithm settings, not
provider API keys.  A deployment adapter resolves those references through its
own secret provider before constructing concrete model clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

SkillConfigSource: TypeAlias = str | Path | Mapping[str, Any] | BaseModel


class SkillRuntimeConfig(BaseModel):
    """Non-secret settings for one local Skill runtime profile."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "default"
    analyzer: str | None = "default"
    optimizer: str | None = "default"
    model_roles: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


__all__ = ["SkillConfigSource", "SkillRuntimeConfig"]
