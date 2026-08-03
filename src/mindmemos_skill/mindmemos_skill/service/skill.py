"""Public transport-neutral facade for local Skill algorithms."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Mapping
from typing import Self

from ..config import SkillConfigSource
from ..errors import SkillCapabilityUnavailableError, SkillConfigurationError, SkillServiceClosedError
from ..typing.operations import (
    SkillAnalysisRequest,
    SkillAnalysisResult,
    SkillOptimizationRequest,
    SkillOptimizationResult,
)
from .protocols import LifecycleHook, SkillAnalyzer, SkillOptimizer, SkillServiceComponents, SkillServiceFactory


class SkillAlgorithms:
    """Pure algorithm API for the currently available Skill operations.

    This class only dispatches requests to the configured algorithm
    capabilities.  It deliberately does not know how the capabilities are
    configured, started, or closed, so it can also be used independently of a
    runtime owner.
    """

    def __init__(
        self,
        *,
        analyzer: SkillAnalyzer | None = None,
        optimizer: SkillOptimizer | None = None,
    ) -> None:
        if analyzer is None and optimizer is None:
            raise SkillConfigurationError("at least one Skill capability must be configured")
        self._analyzer = analyzer
        self._optimizer = optimizer

    @property
    def capabilities(self) -> frozenset[str]:
        """Return the operations available in this algorithm set."""

        capabilities: set[str] = set()
        if self._analyzer is not None:
            capabilities.add("analyze")
        if self._optimizer is not None:
            capabilities.add("optimize")
        return frozenset(capabilities)

    async def analyze(self, request: SkillAnalysisRequest) -> SkillAnalysisResult:
        """Analyze a Skill using the configured local analyzer."""

        if self._analyzer is None:
            raise SkillCapabilityUnavailableError("Skill analysis is not configured")
        return await self._analyzer.analyze(request)

    async def optimize(self, request: SkillOptimizationRequest) -> SkillOptimizationResult:
        """Optimize a Skill using the configured local optimizer."""

        if self._optimizer is None:
            raise SkillCapabilityUnavailableError("Skill optimization is not configured")
        return await self._optimizer.optimize(request)


class MindMemosSkill:
    """Lifecycle and configuration owner for :class:`SkillAlgorithms`.

    The SDK or another deployment composition root should construct this
    runtime through :meth:`from_config` or :meth:`from_env`.  It owns the
    algorithm-resource lifecycle, but not Skill registration, cloud sync, or
    credential persistence.
    """

    def __init__(
        self,
        *,
        algorithms: SkillAlgorithms | None = None,
        analyzer: SkillAnalyzer | None = None,
        optimizer: SkillOptimizer | None = None,
        start: LifecycleHook | None = None,
        close: LifecycleHook | None = None,
    ) -> None:
        if algorithms is not None and (analyzer is not None or optimizer is not None):
            raise SkillConfigurationError("provide algorithms or individual capabilities, not both")
        if algorithms is None:
            algorithms = SkillAlgorithms(analyzer=analyzer, optimizer=optimizer)
        self._algorithms = algorithms
        self._start_hook = start
        self._close_hook = close
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    @classmethod
    def from_config(
        cls,
        config: SkillConfigSource,
        *,
        factory: SkillServiceFactory,
    ) -> Self:
        """Build the facade from an explicit config source.

        The factory, normally supplied by the SDK/local deployment adapter,
        resolves model profiles and secret references.  The facade never opens
        configuration files or stores raw credentials itself.
        """

        return cls._from_components(factory.from_config(config))

    @classmethod
    def from_env(
        cls,
        *,
        factory: SkillServiceFactory,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Build the facade from an explicit environment snapshot.

        Passing a copy instead of allowing algorithms to read ``os.environ``
        directly keeps resolution deterministic and straightforward to test.
        """

        environment = dict(os.environ if environ is None else environ)
        return cls._from_components(factory.from_env(environment))

    @classmethod
    def _from_components(cls, components: SkillServiceComponents) -> Self:
        return cls(
            algorithms=SkillAlgorithms(
                analyzer=components.analyzer,
                optimizer=components.optimizer,
            ),
            start=components.start,
            close=components.close,
        )

    @property
    def capabilities(self) -> frozenset[str]:
        """Return the operations available in the owned algorithm set."""

        return self._algorithms.capabilities

    @property
    def algorithms(self) -> SkillAlgorithms:
        """Return the pure algorithm API owned by this runtime."""

        return self._algorithms

    async def start(self) -> None:
        """Start injected resources once."""

        async with self._lifecycle_lock:
            self._ensure_open()
            if self._started:
                return
            await _run_hook(self._start_hook)
            self._started = True

    async def analyze(self, request: SkillAnalysisRequest) -> SkillAnalysisResult:
        """Start the runtime and delegate analysis to the algorithm API."""

        self._ensure_open()
        if "analyze" not in self.capabilities:
            raise SkillCapabilityUnavailableError("Skill analysis is not configured")
        await self.start()
        return await self._algorithms.analyze(request)

    async def optimize(self, request: SkillOptimizationRequest) -> SkillOptimizationResult:
        """Start the runtime and delegate optimization to the algorithm API."""

        self._ensure_open()
        if "optimize" not in self.capabilities:
            raise SkillCapabilityUnavailableError("Skill optimization is not configured")
        await self.start()
        return await self._algorithms.optimize(request)

    async def close(self) -> None:
        """Close injected resources once."""

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            await _run_hook(self._close_hook)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SkillServiceClosedError("MindMemosSkill is closed")


async def _run_hook(hook: LifecycleHook | None) -> None:
    if hook is None:
        return
    result = hook()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "MindMemosSkill",
    "SkillAlgorithms",
]
