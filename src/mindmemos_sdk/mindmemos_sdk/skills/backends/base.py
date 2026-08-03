"""Backend-neutral asynchronous Skill contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    SkillContentData,
    SkillEvolveData,
    SkillEvolveMode,
    SkillRegisterData,
    SkillSummary,
    SkillSyncData,
    SkillSyncRequestItem,
    SkillVersion,
)


class AsyncSkillBackend(ABC):
    """Execute Skill service operations without owning connection lifecycle."""

    @abstractmethod
    async def register(
        self,
        *,
        name: str,
        content: str,
        version_label: str | None = None,
        parent_version_id: str | None = None,
    ) -> SkillRegisterData:
        """Register one canonical Skill bundle or child version."""

    @abstractmethod
    async def list_skills(self) -> list[SkillSummary]:
        """List backend-managed Skills."""

    @abstractmethod
    async def get_skill(self, cloud_skill_id: str) -> SkillSummary:
        """Return one backend-managed Skill."""

    @abstractmethod
    async def versions_since(
        self,
        cloud_skill_id: str,
        *,
        since: str | None = None,
    ) -> list[SkillVersion]:
        """Return versions newer than an optional timestamp."""

    @abstractmethod
    async def get_content(self, cloud_skill_id: str, version_id: str) -> SkillContentData:
        """Return canonical content for one version."""

    @abstractmethod
    async def evolve(
        self,
        cloud_skill_id: str,
        *,
        mode: SkillEvolveMode = "sync",
    ) -> SkillEvolveData:
        """Run or queue one Skill evolution operation."""

    @abstractmethod
    async def sync(
        self,
        items: list[SkillSyncRequestItem | dict[str, str]],
    ) -> SkillSyncData:
        """Compare local version references with backend published heads."""

    @abstractmethod
    async def delete_skill(self, cloud_skill_id: str) -> None:
        """Remove one backend Skill management relation."""


__all__ = ["AsyncSkillBackend"]
