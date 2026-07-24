"""Protocols and lifecycle components used by the Skill service facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import SkillConfigSource
from ..typing.operations import (
    SkillAnalysisRequest,
    SkillAnalysisResult,
    SkillOptimizationRequest,
    SkillOptimizationResult,
)

LifecycleHook = Callable[[], Awaitable[None] | None]


@runtime_checkable
class SkillAnalyzer(Protocol):
    """Algorithm capability used by :class:`SkillAlgorithms.analyze`."""

    async def analyze(self, request: SkillAnalysisRequest) -> SkillAnalysisResult:
        """Analyze one Skill and its local execution evidence."""
        ...


@runtime_checkable
class SkillOptimizer(Protocol):
    """Algorithm capability used by :class:`SkillAlgorithms.optimize`."""

    async def optimize(self, request: SkillOptimizationRequest) -> SkillOptimizationResult:
        """Produce the selected optimized Skill candidate."""
        ...


@dataclass(frozen=True, slots=True)
class SkillServiceComponents:
    """Resolved local components returned by a composition-layer factory."""

    analyzer: SkillAnalyzer | None = None
    optimizer: SkillOptimizer | None = None
    start: LifecycleHook | None = None
    close: LifecycleHook | None = None


@runtime_checkable
class SkillServiceFactory(Protocol):
    """Build local algorithm components from deployment-owned configuration."""

    def from_config(self, config: SkillConfigSource) -> SkillServiceComponents:
        """Resolve a concrete runtime from an explicit config source."""
        ...

    def from_env(self, environ: Mapping[str, str]) -> SkillServiceComponents:
        """Resolve a concrete runtime from an explicit environment mapping."""
        ...


__all__ = [
    "LifecycleHook",
    "SkillAnalyzer",
    "SkillOptimizer",
    "SkillServiceComponents",
    "SkillServiceFactory",
]
