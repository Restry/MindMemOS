"""HTTP implementation of the asynchronous Skill backend."""

from __future__ import annotations

from urllib.parse import quote

from ...connections import HttpConnection
from ..models import (
    SkillContentData,
    SkillEvolveData,
    SkillEvolveMode,
    SkillListData,
    SkillRegisterData,
    SkillSummary,
    SkillSyncData,
    SkillSyncRequestItem,
    SkillVersion,
    SkillVersionsData,
)
from .base import AsyncSkillBackend


def _path_part(value: str) -> str:
    return quote(value, safe="")


class HttpSkillBackend(AsyncSkillBackend):
    """Execute the public ``/v1/skills/*`` contract over HTTP."""

    def __init__(self, connection: HttpConnection) -> None:
        self._connection = connection

    @property
    def _transport(self):
        return self._connection.transport

    async def register(
        self,
        *,
        name: str,
        content: str,
        version_label: str | None = None,
        parent_version_id: str | None = None,
    ) -> SkillRegisterData:
        body = {"name": name, "content": content}
        if version_label is not None:
            body["version_label"] = version_label
        if parent_version_id is not None:
            body["parent_version_id"] = parent_version_id
        envelope = await self._transport.post_envelope("/v1/skills/register", json=body)
        return SkillRegisterData.model_validate(envelope.data or {})

    async def list_skills(self) -> list[SkillSummary]:
        envelope = await self._transport.get_envelope("/v1/skills")
        return SkillListData.model_validate(envelope.data or {}).skills

    async def get_skill(self, cloud_skill_id: str) -> SkillSummary:
        envelope = await self._transport.post_envelope(
            f"/v1/skills/{_path_part(cloud_skill_id)}/get",
            json=None,
        )
        return SkillSummary.model_validate(envelope.data or {})

    async def versions_since(
        self,
        cloud_skill_id: str,
        *,
        since: str | None = None,
    ) -> list[SkillVersion]:
        params = {"since": since} if since else None
        envelope = await self._transport.get_envelope(
            f"/v1/skills/{_path_part(cloud_skill_id)}/versions",
            params=params,
        )
        return SkillVersionsData.model_validate(envelope.data or {}).versions

    async def get_content(self, cloud_skill_id: str, version_id: str) -> SkillContentData:
        envelope = await self._transport.get_envelope(
            f"/v1/skills/{_path_part(cloud_skill_id)}/versions/{_path_part(version_id)}/content",
        )
        return SkillContentData.model_validate(envelope.data or {})

    async def evolve(
        self,
        cloud_skill_id: str,
        *,
        mode: SkillEvolveMode = "sync",
    ) -> SkillEvolveData:
        envelope = await self._transport.post_envelope(
            "/v1/skills/evolve",
            json={"cloud_skill_id": cloud_skill_id, "mode": mode},
        )
        data = SkillEvolveData.model_validate(envelope.data or {})
        return data.model_copy(update={"status": envelope.code or data.status})

    async def sync(
        self,
        items: list[SkillSyncRequestItem | dict[str, str]],
    ) -> SkillSyncData:
        body = [item.model_dump() if isinstance(item, SkillSyncRequestItem) else item for item in items]
        envelope = await self._transport.post_envelope("/v1/skills/sync", json=body)
        return SkillSyncData.model_validate(envelope.data or {})

    async def delete_skill(self, cloud_skill_id: str) -> None:
        await self._transport.post_envelope(
            f"/v1/skills/{_path_part(cloud_skill_id)}/delete",
            json=None,
        )


__all__ = ["HttpSkillBackend"]
