"""Unified lifecycle owner for in-process MindMemOS usage.

This module deliberately does not depend on FastAPI. HTTP hosting and Python
callers use the same asynchronous runtime owner.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Self

from .config import MemoryConfig, get_config, init_config, init_config_from_env
from .infra.tasking import InMemoryTaskBackend, TaskClient, TaskHandlerRegistry
from .infra.tasking.errors import TaskBackendNotStarted
from .infra.tasking.ports import TaskBackend
from .infra.tasking.registry import get_handler_registry
from .infra.vector_store import VectorDBService
from .llm import close_llm_clients, init_embed_client, init_llm_client, validate_embedding_dimension
from .logging import configure_logging, configure_tracing, get_logger
from .persistence import MemoryPersistence, ensure_database_schema
from .persistence.v2 import ENTITY_TABLE, MEMORY_TABLE, SOURCE_TABLE, build_v2_registry
from .service.memory import MixedMemoryService
from .service.ports.memory import MemoryService
from .service.ports.skill import SkillService

logger = get_logger(__name__)


class MindMemOSState(StrEnum):
    """Lifecycle state exposed for hosting integrations and diagnostics."""

    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


_owner_lock = Lock()
_active_runtime: MindMemOS | None = None


class MindMemOS:
    """Own MindMemOS process resources for one asynchronous application.

    The current config, task backend, LLM and telemetry registries are process-global,
    so only one ``MindMemOS`` instance may be running at a time. All operations
    and shutdown must happen on the event loop that started the instance.

    Args:
        config_name: Named config used when ``config_path`` is absent.
        config_path: Explicit YAML config path.
        start_workers: Start the injected task backend. No task handlers are
            registered automatically.
        task_backend: Optional backend owned by this runtime.
        task_handlers: Optional handler registry; defaults to the process registry.
        load_config_from_env: Resolve config through ``MINDMEMOS_CONFIG_PATH``
            or ``MINDMEMOS_CONFIG_NAME`` at startup.
    """

    def __init__(
        self,
        *,
        config_name: str = "dev",
        config_path: str | Path | None = None,
        start_workers: bool = False,
        load_config_from_env: bool = False,
        task_backend: TaskBackend | None = None,
        task_handlers: TaskHandlerRegistry | None = None,
        vector_db_service: VectorDBService | None = None,
    ) -> None:
        if load_config_from_env and config_path is not None:
            raise ValueError("config_path cannot be combined with load_config_from_env=True")

        self._config_name = config_name
        self._config_path = Path(config_path).expanduser() if config_path is not None else None
        self._start_workers = start_workers
        self._load_config_from_env = load_config_from_env
        self._state = MindMemOSState.NEW
        self._config_source: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._database_started = False
        self._llm_started = False
        self._task_backend_started = False
        self._observability_started = False
        self._vector_db_service = vector_db_service
        self._memory_persistence: MemoryPersistence | None = (
            MemoryPersistence(vector_db_service) if vector_db_service is not None else None
        )
        resolved_task_handlers = task_handlers or get_handler_registry()
        if task_backend is None and start_workers:
            task_backend = InMemoryTaskBackend(resolved_task_handlers)
        self._task_client: TaskClient | None = (
            TaskClient(task_backend, resolved_task_handlers) if task_backend is not None else None
        )
        self._memory_service: MemoryService | None = None
        self._skill_service: SkillService | None = None

    @classmethod
    def from_env(
        cls,
        *,
        start_workers: bool = True,
        task_backend: TaskBackend | None = None,
        task_handlers: TaskHandlerRegistry | None = None,
    ) -> Self:
        """Build a runtime whose config is resolved from the environment."""

        return cls(
            start_workers=start_workers,
            load_config_from_env=True,
            task_backend=task_backend,
            task_handlers=task_handlers,
        )

    @classmethod
    def from_config(
        cls,
        config_path: str | Path,
        *,
        start_workers: bool = False,
        task_backend: TaskBackend | None = None,
        task_handlers: TaskHandlerRegistry | None = None,
    ) -> Self:
        """Build a runtime from an explicit YAML config path."""

        return cls(
            config_path=config_path,
            start_workers=start_workers,
            task_backend=task_backend,
            task_handlers=task_handlers,
        )

    @property
    def state(self) -> MindMemOSState:
        """Return the current lifecycle state."""

        return self._state

    @property
    def is_running(self) -> bool:
        """Return whether startup completed successfully."""

        return self._state is MindMemOSState.RUNNING

    @property
    def config_source(self) -> str | None:
        """Return the config name or path selected during startup."""

        return self._config_source

    @property
    def config(self) -> MemoryConfig:
        """Return the active resolved config."""

        self._require_running()
        return get_config()

    @property
    def task_client(self) -> TaskClient:
        """Return the runtime-owned task client."""

        self._require_running()
        if self._task_client is None:
            raise TaskBackendNotStarted("task backend is not configured for this runtime")
        return self._task_client

    @property
    def memory(self) -> MemoryService:
        """Return the transport-neutral memory service."""

        self._require_running()
        if self._memory_service is None:
            if self._memory_persistence is None:
                raise RuntimeError("memory persistence was not initialized")
            self._memory_service = MixedMemoryService.from_config(
                self._memory_persistence,
                config=get_config(),
                task_client=self._task_client,
            )
        return self._memory_service

    @property
    def skill(self) -> SkillService:
        """Return the transport-neutral skill service."""

        self._require_running()
        if self._skill_service is None:
            self._skill_service = SkillService()
        return self._skill_service

    async def start(self) -> Self:
        """Initialize config and shared process resources exactly once."""

        async with self._lifecycle_lock:
            if self._state is MindMemOSState.RUNNING:
                self._require_owner_loop()
                return self
            if self._state is not MindMemOSState.NEW:
                raise RuntimeError(f"MindMemOS cannot start from state {self._state.value!r}")

            self._claim_process_owner()
            self._state = MindMemOSState.STARTING
            self._loop = asyncio.get_running_loop()
            try:
                self._config_source = self._initialize_config()
                cfg = get_config()
                configure_logging()
                self._observability_started = configure_tracing(cfg.observability) is not None

                if self._vector_db_service is None:
                    dimensions = {
                        endpoint.dimensions
                        for endpoint in cfg.embed_model_router.endpoints
                        if endpoint.dimensions is not None
                    }
                    if len(dimensions) != 1:
                        raise RuntimeError(
                            "vanilla runtime requires one explicit embedding dimension "
                            "across embed_model_router endpoints"
                        )
                    tables = build_v2_registry(
                        vector_dimensions=next(iter(dimensions)),
                        sparse_hash_dim=cfg.algo_config.text_processing.sparse_hash_dim,
                    )
                    self._database_started = True
                    self._vector_db_service = await ensure_database_schema(
                        cfg.database,
                        tables,
                        node_tables={
                            "Memory": MEMORY_TABLE,
                            "Entity": ENTITY_TABLE,
                            "Source": SOURCE_TABLE,
                        },
                    )
                    self._memory_persistence = MemoryPersistence(self._vector_db_service)

                self._llm_started = True
                init_llm_client()
                init_embed_client()
                await validate_embedding_dimension()

                if self._start_workers and self._task_client is not None:
                    self._task_backend_started = True
                    await self._task_client.backend.start()

                self._state = MindMemOSState.RUNNING
                logger.info(
                    "mindmemos runtime started",
                    config_source=self._config_source,
                    task_backend_started=self._task_backend_started,
                    observability_started=self._observability_started,
                )
                return self
            except BaseException:
                self._state = MindMemOSState.FAILED
                await self._shutdown_resources()
                self._release_process_owner()
                raise

    async def close(self) -> None:
        """Stop shared resources in reverse startup order.

        Shutdown is best-effort so one failing client cannot prevent the other
        process resources from being released.
        """

        async with self._lifecycle_lock:
            if self._state is MindMemOSState.CLOSED:
                return
            if self._state is MindMemOSState.NEW:
                self._state = MindMemOSState.CLOSED
                return
            if self._state is MindMemOSState.STARTING:
                raise RuntimeError("MindMemOS cannot close while startup is in progress")

            self._require_owner_loop()
            self._state = MindMemOSState.STOPPING
            await self._shutdown_resources()
            self._state = MindMemOSState.CLOSED
            self._release_process_owner()
            logger.info("mindmemos runtime stopped")

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.close()

    def _initialize_config(self) -> str:
        if self._load_config_from_env:
            return init_config_from_env()
        init_config(config_name=self._config_name, config_path=self._config_path)
        return str(self._config_path or self._config_name)

    async def _shutdown_resources(self) -> None:
        if self._task_backend_started and self._task_client is not None:
            await self._close_resource("tasks", self._task_client.backend.close)
            self._task_backend_started = False
        if self._llm_started:
            await self._close_resource("llm clients", close_llm_clients)
            self._llm_started = False
        if self._database_started and self._vector_db_service is not None:
            await self._close_resource("database", self._vector_db_service.close)
            self._database_started = False
        if self._observability_started:
            from .infra import shutdown_tracer_provider

            shutdown_tracer_provider()
            self._observability_started = False

    async def _close_resource(self, name: str, closer) -> None:
        try:
            await closer()
        except Exception:  # noqa: BLE001 - shutdown must continue
            logger.warning("failed to close runtime resource", resource=name, exc_info=True)

    def _require_running(self) -> None:
        if self._state is not MindMemOSState.RUNNING:
            raise RuntimeError("MindMemOS is not running; use 'async with MindMemOS(...)' or await start()")
        self._require_owner_loop()

    def _require_owner_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("MindMemOS must be accessed from its running event loop") from exc
        if self._loop is not None and loop is not self._loop:
            raise RuntimeError("MindMemOS must be accessed and closed from the event loop that started it")

    def _claim_process_owner(self) -> None:
        global _active_runtime
        with _owner_lock:
            if _active_runtime is not None and _active_runtime is not self:
                raise RuntimeError("only one MindMemOS runtime may be active in a process")
            _active_runtime = self

    def _release_process_owner(self) -> None:
        global _active_runtime
        with _owner_lock:
            if _active_runtime is self:
                _active_runtime = None


def get_task_client() -> TaskClient:
    """Return the active runtime's task client through a thin global accessor."""

    if _active_runtime is None:
        raise TaskBackendNotStarted("MindMemOS runtime is not active")
    return _active_runtime.task_client
