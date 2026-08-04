"""Data contracts for local Skill analysis and optimization."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .skill import Skill
from .trajectory import Trajectory


class SkillFinding(BaseModel):
    """One actionable observation produced while analyzing a Skill."""

    model_config = ConfigDict(extra="forbid")

    category: str
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    evidence: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillAnalysisRequest(BaseModel):
    """Inputs needed to analyze one Skill without SDK or cloud state."""

    model_config = ConfigDict(extra="forbid")

    skill: Skill
    trajectories: list[Trajectory] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class SkillAnalysisResult(BaseModel):
    """Transport-neutral result of one Skill analysis."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[SkillFinding] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillOptimizationRequest(BaseModel):
    """Inputs needed to produce a locally optimized Skill candidate."""

    model_config = ConfigDict(extra="forbid")

    skill: Skill
    trajectories: list[Trajectory] = Field(default_factory=list)
    analysis: SkillAnalysisResult | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class SkillOptimizationResult(BaseModel):
    """Selected optimized Skill plus optional candidate and audit information."""

    model_config = ConfigDict(extra="forbid")

    skill: Skill
    changed: bool
    candidates: list[Skill] = Field(default_factory=list)
    analysis: SkillAnalysisResult | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "SkillAnalysisRequest",
    "SkillAnalysisResult",
    "SkillFinding",
    "SkillOptimizationRequest",
    "SkillOptimizationResult",
]
