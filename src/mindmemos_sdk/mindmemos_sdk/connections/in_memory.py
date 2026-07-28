"""Shared in-memory runtime connection.

The connection is named for its transport mode rather than the current runtime
package. ``mindmemos_lite`` is loaded lazily only when an owned connection is
opened.
"""

from __future__ import annotations

import importlib
from typing import Any

from ..config import InMemoryConnectionConfig
from ..errors import LiteUnavailableError
from .base import AsyncConnection


class InMemoryConnection(AsyncConnection):
    """Own or borrow one transport-neutral in-process runtime."""

    def __init__(
        self,
        config: InMemoryConnectionConfig,
        *,
        runtime: Any | None = None,
        owns_runtime: bool | None = None,
    ) -> None:
        self.config = config
        self._runtime = runtime
        self._owns_runtime = runtime is None if owns_runtime is None else owns_runtime
        self._opened = runtime is not None and bool(getattr(runtime, "is_running", False))
        self._closed = False

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory", "skills"})

    @property
    def runtime(self) -> Any:
        if not self._opened or self._runtime is None:
            raise RuntimeError("in-memory connection is not open")
        return self._runtime

    async def open(self) -> None:
        if self._closed:
            raise RuntimeError("in-memory connection is closed")
        if self._opened:
            return
        if self._runtime is None:
            self._runtime = self._build_runtime()
            await self._runtime.start()
        elif not bool(getattr(self._runtime, "is_running", False)):
            if self._owns_runtime:
                await self._runtime.start()
            else:
                raise LiteUnavailableError("borrowed in-memory runtime must already be running")
        self._opened = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._opened and self._owns_runtime and self._runtime is not None:
            await self._runtime.close()
        self._opened = False

    def _build_runtime(self) -> Any:
        if self.config.runtime != "mindmemos_lite":
            raise LiteUnavailableError(f"unsupported in-memory runtime: {self.config.runtime}")
        try:
            runtime_module = importlib.import_module("mindmemos_lite.runtime")
            runtime_type = runtime_module.MindMemOS
        except (ImportError, AttributeError) as exc:
            raise LiteUnavailableError(
                "mindmemos_lite.runtime.MindMemOS is required for the configured in-memory connection"
            ) from exc

        if self.config.config_path is not None:
            return runtime_type.from_config(
                self.config.config_path,
                start_workers=self.config.start_workers,
            )
        if self.config.load_config_from_env:
            return runtime_type.from_env(start_workers=self.config.start_workers)
        return runtime_type(
            config_name=self.config.config_name,
            start_workers=self.config.start_workers,
        )


__all__ = ["InMemoryConnection"]
