"""Asynchronous root SDK client and composition owner."""

from __future__ import annotations

import asyncio
from typing import Self

from .composition import ConnectionPool, build_connections, build_memory_backend, build_skill_backend
from .config import ConfigManager, SDKConfig
from .connections import AsyncConnection
from .memory import AsyncMemoryClient
from .memory.core import MemoryDefaults
from .skills import AsyncSkillClient, SkillManager


class AsyncMindMemOSClient:
    """Configure, expose and close all asynchronous SDK resource clients."""

    def __init__(
        self,
        *,
        config: SDKConfig | None = None,
        config_manager: ConfigManager | None = None,
        connections: dict[str, AsyncConnection] | None = None,
    ) -> None:
        manager = config_manager or ConfigManager()
        self._config = config or manager.load_or_default()
        self._config_manager = manager
        available_connections = connections or build_connections(self._config)
        selected_names = {
            self._config.clients.memory.connection,
            self._config.clients.skills.connection,
        }
        try:
            selected_connections = {
                name: available_connections[name]
                for name in selected_names
            }
        except KeyError as exc:
            raise ValueError(f"unknown SDK connection: {exc.args[0]!r}") from exc
        self._pool = ConnectionPool(selected_connections)
        self._started = False
        self._closed = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None

        memory_connection = self._pool.get(
            self._config.clients.memory.connection,
            capability="memory",
        )
        skill_connection = self._pool.get(
            self._config.clients.skills.connection,
            capability="skills",
        )
        local_skills = SkillManager.from_config_manager(manager)
        defaults = self._config.defaults
        memory_config = self._config.memory
        self.memory = AsyncMemoryClient(
            build_memory_backend(memory_connection, defaults=defaults),
            default_user_id=defaults.user_id,
            default_app_id=defaults.app_id,
            default_agent_id=defaults.agent_id,
            default_session_id=defaults.session_id,
            memory_defaults=MemoryDefaults(
                user_id=defaults.user_id,
                app_id=defaults.app_id,
                agent_id=defaults.agent_id,
                session_id=defaults.session_id,
                add_mode=memory_config.add_mode,
                add_default_role=memory_config.add_default_role,
                add_auto_skill_context=memory_config.add_auto_skill_context,
                search_top_k=memory_config.search_top_k,
                search_strategy=memory_config.search_strategy,
                search_rerank=memory_config.search_rerank,
                search_score_threshold=memory_config.search_score_threshold,
                search_filters=memory_config.search_filters,
                get_top_k=memory_config.get_top_k,
                get_filters=memory_config.get_filters,
                feedback_mode=memory_config.feedback_mode,
                dreaming_mode=memory_config.dreaming_mode,
            ),
        )
        self.skills = AsyncSkillClient(
            build_skill_backend(skill_connection, defaults=defaults),
            local=local_skills,
        )

    async def start(self) -> Self:
        if self._closed:
            raise RuntimeError("SDK client is closed")
        loop = asyncio.get_running_loop()
        if self._started:
            if loop is not self._owner_loop:
                raise RuntimeError("SDK client cannot be used across event loops")
            return self
        self._owner_loop = loop
        try:
            await self._pool.open()
        except BaseException:
            self._owner_loop = None
            raise
        self._started = True
        return self

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owner_loop is not None and asyncio.get_running_loop() is not self._owner_loop:
            raise RuntimeError("SDK client cannot be closed from another event loop")
        self._closed = True
        await self._pool.aclose()
        self._started = False

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["AsyncMindMemOSClient"]
