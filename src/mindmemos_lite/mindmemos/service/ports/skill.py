"""Application-facing skill service port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeAlias

from ..schema import (
    EvolveSkillRequest,
    RegisterSkillRequest,
    RegisterSkillResult,
    RequestContext,
    SkillContent,
    SkillEvolveResult,
    SkillSummary,
    SkillSyncResult,
    SkillVersion,
    SyncSkillsRequest,
)


class SkillService(Protocol):
    """Transport-neutral contract for every public skill endpoint.

    ``list_versions`` and ``get_version_content`` use explicit names in the
    lite interface; they correspond to the original service's ``versions``
    and ``content`` methods.  The port returns domain results, not an HTTP
    ``ApiResponse`` envelope.
    """

    async def register(self, context: RequestContext, request: RegisterSkillRequest) -> RegisterSkillResult:
        """Register one canonical skill bundle or a child version."""

        ...

    async def list_skills(self, context: RequestContext) -> tuple[SkillSummary, ...]:
        """List project-scoped managed skills."""

        ...

    async def get_skill(self, context: RequestContext, cloud_skill_id: str) -> SkillSummary:
        """Return one skill summary and its published head when available."""

        ...

    async def list_versions(
        self,
        context: RequestContext,
        cloud_skill_id: str,
        *,
        since: datetime | None = None,
    ) -> tuple[SkillVersion, ...]:
        """Return version metadata after an optional timestamp."""

        ...

    async def get_version_content(
        self,
        context: RequestContext,
        cloud_skill_id: str,
        version_id: str,
    ) -> SkillContent:
        """Return one version's metadata and canonical bundle content."""

        ...

    async def delete_skill(self, context: RequestContext, cloud_skill_id: str) -> None:
        """Unmanage all versions of one project-scoped skill."""

        ...

    async def evolve(self, context: RequestContext, request: EvolveSkillRequest) -> SkillEvolveResult:
        """Run or queue one skill self-evolution pass."""

        ...

    async def sync(self, context: RequestContext, request: SyncSkillsRequest) -> tuple[SkillSyncResult, ...]:
        """Compare local versions with project published heads."""

        ...


SkillPort: TypeAlias = SkillService


__all__ = ["SkillPort", "SkillService"]
