"""In-memory implementation of the asynchronous Skill backend."""

from __future__ import annotations

import dataclasses
import importlib
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from ...config import DefaultsConfig
from ...connections import InMemoryConnection
from ...errors import LiteExecutionError, LiteUnavailableError
from ..models import (
    SkillContentData,
    SkillEvolveData,
    SkillEvolveMode,
    SkillRegisterData,
    SkillSummary,
    SkillSyncData,
    SkillSyncRequestItem,
    SkillSyncResult,
    SkillVersion,
)
from .base import AsyncSkillBackend


def _load_schema() -> Any:
    try:
        return importlib.import_module("mindmemos_lite.service.schema")
    except (ImportError, AttributeError) as exc:
        raise LiteUnavailableError(
            "mindmemos_lite with the transport-neutral Skill service schema is required"
        ) from exc


def _model_payload(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"since must be an ISO-8601 datetime: {value!r}") from exc


class InMemorySkillBackend(AsyncSkillBackend):
    """Map SDK Skill calls to a borrowed transport-neutral runtime service."""

    _REQUIRED_METHODS = (
        "register",
        "list_skills",
        "get_skill",
        "list_versions",
        "get_version_content",
        "delete_skill",
        "evolve",
        "sync",
    )

    def __init__(
        self,
        connection: InMemoryConnection,
        *,
        defaults: DefaultsConfig | None = None,
    ) -> None:
        self._connection = connection
        self._defaults = defaults or DefaultsConfig()
        self._schema = _load_schema()

    @property
    def _service(self) -> Any:
        try:
            service = self._connection.runtime.skill
        except Exception as exc:
            raise LiteUnavailableError("in-memory runtime does not expose a runnable Skill service") from exc
        missing = [name for name in self._REQUIRED_METHODS if not callable(getattr(service, name, None))]
        if missing:
            raise LiteUnavailableError(
                "in-memory Skill service is missing required operations: " + ", ".join(missing)
            )
        return service

    def _context(self) -> Any:
        config = self._connection.config
        defaults = self._defaults
        return self._schema.RequestContext(
            request_id=str(uuid.uuid4()),
            account_id=config.account_id,
            project_id=config.project_id,
            api_key_uuid=config.api_key_uuid,
            user_id=defaults.user_id,
            app_id=defaults.app_id,
            agent_id=defaults.agent_id,
            session_id=defaults.session_id,
        )

    async def _call(self, operation: str, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as exc:
            raise LiteExecutionError(operation=operation, message=str(exc)) from exc

    async def register(
        self,
        *,
        name: str,
        content: str,
        version_label: str | None = None,
        parent_version_id: str | None = None,
    ) -> SkillRegisterData:
        request = self._schema.RegisterSkillRequest(
            name=name,
            content=content,
            version_label=version_label,
            parent_version_id=parent_version_id,
        )
        result = await self._call("skill.register", self._service.register(self._context(), request))
        return SkillRegisterData.model_validate(_model_payload(result))

    async def list_skills(self) -> list[SkillSummary]:
        result = await self._call("skill.list", self._service.list_skills(self._context()))
        return [SkillSummary.model_validate(_model_payload(item)) for item in result]

    async def get_skill(self, cloud_skill_id: str) -> SkillSummary:
        result = await self._call("skill.get", self._service.get_skill(self._context(), cloud_skill_id))
        return SkillSummary.model_validate(_model_payload(result))

    async def versions_since(
        self,
        cloud_skill_id: str,
        *,
        since: str | None = None,
    ) -> list[SkillVersion]:
        result = await self._call(
            "skill.versions",
            self._service.list_versions(
                self._context(),
                cloud_skill_id,
                since=_parse_since(since),
            ),
        )
        return [SkillVersion.model_validate(_model_payload(item)) for item in result]

    async def get_content(self, cloud_skill_id: str, version_id: str) -> SkillContentData:
        result = await self._call(
            "skill.content",
            self._service.get_version_content(self._context(), cloud_skill_id, version_id),
        )
        return SkillContentData.model_validate(_model_payload(result))

    async def evolve(
        self,
        cloud_skill_id: str,
        *,
        mode: SkillEvolveMode = "sync",
    ) -> SkillEvolveData:
        request = self._schema.EvolveSkillRequest(cloud_skill_id=cloud_skill_id, mode=mode)
        result = await self._call("skill.evolve", self._service.evolve(self._context(), request))
        return SkillEvolveData.model_validate(_model_payload(result))

    async def sync(
        self,
        items: list[SkillSyncRequestItem | dict[str, str]],
    ) -> SkillSyncData:
        mapped_items = []
        for item in items:
            payload = item.model_dump() if isinstance(item, SkillSyncRequestItem) else item
            mapped_items.append(self._schema.SkillSyncItem(**payload))
        request = self._schema.SyncSkillsRequest(items=tuple(mapped_items))
        result = await self._call("skill.sync", self._service.sync(self._context(), request))
        return SkillSyncData(results=[SkillSyncResult.model_validate(_model_payload(item)) for item in result])

    async def delete_skill(self, cloud_skill_id: str) -> None:
        await self._call("skill.delete", self._service.delete_skill(self._context(), cloud_skill_id))


__all__ = ["InMemorySkillBackend"]
