"""HTTP-facing search pipeline."""

from __future__ import annotations

import asyncio
from typing import Any

from ...components.searcher import SearchFinalFilter
from ...config import get_config
from ...llm import RerankClient
from ...typing import MemoryRequestContext, SearchPipelineInput, SearchPipelineResult
from ..base import MemoryDbPipelineMixin
from ..registry import register
from .agentic.wrapper import AgenticSearchWrapper
from .base import SearchEngine
from .default import DefaultSearchEngine
from .schema import SchemaSearchEngine
from .vanilla import VanillaSearchEngine

_DEFAULT_ENGINE_NAMES = frozenset({"default", "vanilla", "schema"})


@register(type="search", name="search_pipeline")
class SearchPipelineImpl(MemoryDbPipelineMixin):
    """Select a search engine, optionally wrap it in agentic orchestration, then final-filter."""

    def __init__(
        self,
        *,
        engines: dict[str, SearchEngine] | None = None,
        agentic_wrapper: AgenticSearchWrapper | None = None,
        final_filter: SearchFinalFilter | None = None,
        rerank_client: RerankClient | None = None,
        search_timeout_seconds: float = 8.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._engines = dict(engines or {})
        self._use_default_engines = engines is None
        self._agentic = agentic_wrapper
        self._search_timeout_seconds = max(0.01, float(search_timeout_seconds))
        if final_filter is not None:
            self._final_filter = final_filter
        else:
            self._final_filter = SearchFinalFilter(
                rerank_client=rerank_client,
                rerank_client_factory=None if rerank_client is not None else _optional_rerank_client,
            )

    async def search(self, inp: SearchPipelineInput, context: MemoryRequestContext) -> SearchPipelineResult:
        """Run search under a total deadline; timeout degrades to no injection."""

        try:
            return await asyncio.wait_for(self._search(inp, context), timeout=self._search_timeout_seconds)
        except TimeoutError:
            return SearchPipelineResult(
                status="ok",
                memories=[],
                quality_trace={
                    "intent": "unknown",
                    "candidate_ids": [],
                    "filtered": [],
                    "final_ids": [],
                    "degraded_reason": "search_deadline_exceeded",
                    "elapsed_ms": round(self._search_timeout_seconds * 1000, 2),
                },
            )

    async def _search(self, inp: SearchPipelineInput, context: MemoryRequestContext) -> SearchPipelineResult:
        """Run the selected engine and final decision layer."""

        strategy = inp.search_pipeline
        engine = self._engine(strategy)
        if engine is None:
            available = ", ".join(sorted(self._available_engine_names()))
            raise ValueError(f"Unknown search strategy {strategy!r}. Available strategies: {available}")

        if inp.agentic:
            candidates = await self._agentic_wrapper().run(inp, context, engine)
        else:
            candidates = await engine.search_candidates(inp, context)
        quality_trace: dict[str, Any] = {}
        memories = await self._final_filter.apply(
            query=inp.query,
            candidates=candidates,
            top_k=inp.top_k,
            rerank=inp.rerank and _strategy_allows_rerank(strategy),
            score_threshold=inp.score_threshold,
            quality_trace=quality_trace,
        )
        return SearchPipelineResult(status="ok", memories=memories, quality_trace=quality_trace)

    def _engine(self, name: str) -> SearchEngine | None:
        engine = self._engines.get(name)
        if engine is not None or not self._use_default_engines:
            return engine
        if name not in _DEFAULT_ENGINE_NAMES:
            return None

        common = {"db_reader": self.db_reader, "db_writer": self.db_writer}
        if name == "default":
            engine = DefaultSearchEngine(**common)
        elif name == "vanilla":
            engine = VanillaSearchEngine(**common)
        else:
            engine = SchemaSearchEngine(**common)
        self._engines[name] = engine
        return engine

    def _agentic_wrapper(self) -> AgenticSearchWrapper:
        if self._agentic is None:
            self._agentic = AgenticSearchWrapper()
        return self._agentic

    def _available_engine_names(self) -> set[str]:
        if self._use_default_engines:
            return set(_DEFAULT_ENGINE_NAMES)
        return set(self._engines)


def _optional_rerank_client() -> RerankClient | None:
    try:
        from ...llm import get_rerank_client

        return get_rerank_client()
    except Exception:
        return None


def _strategy_allows_rerank(strategy: str) -> bool:
    if strategy != "vanilla":
        return True
    return get_config().algo_config.search.vanilla.use_reranker
