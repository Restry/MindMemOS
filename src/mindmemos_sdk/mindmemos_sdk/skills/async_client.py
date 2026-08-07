"""Asynchronous Skill service client backed by the MindMemOS HTTP API."""

from __future__ import annotations

from typing import Any

from ..config import ConfigManager
from ..config.models import HttpConnectionConfig
from ..connections import AsyncConnection, HttpConnection
from .backends import AsyncSkillBackend, HttpSkillBackend
from .manager import SkillManager
from .models import (
    SkillContentData,
    SkillEvolveData,
    SkillEvolveMode,
    SkillRegisterData,
    SkillSummary,
    SkillSyncData,
    SkillSyncRequestItem,
    SkillVersion,
)


class AsyncSkillClient:
    """Thin public facade over an :class:`AsyncSkillBackend`.

    This client represents backend service calls only. Local immutable versions,
    active pointers, import/export and outbox state continue to belong to
    :class:`mindmemos_sdk.skills.SkillManager`.
    """

    def __init__(
        self,
        backend: AsyncSkillBackend,
        *,
        local: SkillManager | None = None,
        connection: AsyncConnection | None = None,
    ) -> None:
        if not isinstance(backend, AsyncSkillBackend):
            raise TypeError("backend must be an AsyncSkillBackend")
        self._backend = backend
        self._local = local
        self._connection = connection

    @classmethod
    def from_http(
        cls,
        transport: Any,
        *,
        config_manager: ConfigManager | None = None,
        owns_transport: bool = False,
    ) -> AsyncSkillClient:
        """Build an API-mode client with SDK-owned local version management."""

        manager = config_manager or ConfigManager()
        connection = HttpConnection(
            HttpConnectionConfig(base_url="https://external.invalid"),
            transport=transport,
            owns_transport=owns_transport,
        )
        return cls(
            HttpSkillBackend(connection),
            local=SkillManager.from_config_manager(manager),
            connection=connection,
        )

    @property
    def local(self) -> SkillManager:
        """Return the SDK-owned local Skill version manager."""

        if self._local is None:
            raise RuntimeError("SDK local Skill management is not configured")
        return self._local

    async def register(
        self,
        *,
        name: str,
        content: str,
        version_label: str | None = None,
        parent_version_id: str | None = None,
    ) -> SkillRegisterData:
        return await self._backend.register(
            name=name,
            content=content,
            version_label=version_label,
            parent_version_id=parent_version_id,
        )

    async def list_skills(self) -> list[SkillSummary]:
        return await self._backend.list_skills()

    async def get_skill(self, cloud_skill_id: str) -> SkillSummary:
        return await self._backend.get_skill(cloud_skill_id)

    async def versions_since(
        self,
        cloud_skill_id: str,
        *,
        since: str | None = None,
    ) -> list[SkillVersion]:
        return await self._backend.versions_since(cloud_skill_id, since=since)

    async def get_content(self, cloud_skill_id: str, version_id: str) -> SkillContentData:
        return await self._backend.get_content(cloud_skill_id, version_id)

    async def evolve(
        self,
        cloud_skill_id: str,
        *,
        mode: SkillEvolveMode = "sync",
    ) -> SkillEvolveData:
        return await self._backend.evolve(cloud_skill_id, mode=mode)

    async def sync(
        self,
        items: list[SkillSyncRequestItem | dict[str, str]],
    ) -> SkillSyncData:
        return await self._backend.sync(items)

    async def delete_skill(self, cloud_skill_id: str) -> None:
        await self._backend.delete_skill(cloud_skill_id)

    async def aclose(self) -> None:
        if self._connection is not None:
            await self._connection.aclose()


__all__ = ["AsyncSkillClient"]
