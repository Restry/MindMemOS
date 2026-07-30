"""HTTP-facing search pipeline."""

from __future__ import annotations

from math import isfinite
from typing import Any

from ...components.searcher import MemoryRetentionSelector, SearchFinalFilter
from ...config import get_config
from ...config.algo.search.retention import MemoryRetentionConfig
from ...llm import RerankClient
from ...typing import (
    MemoryRequestContext,
    MemorySearchResultItem,
    SearchGraphProvenance,
    SearchPipelineInput,
    SearchPipelineResult,
    SearchRelevance,
)
from ..base import MemoryDbPipelineMixin
from ..registry import register
from .agentic.wrapper import AgenticSearchWrapper
from .base import SearchEngine, SearchEngineOptions
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
        retention_config: MemoryRetentionConfig | None = None,
        retention_selector: MemoryRetentionSelector | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._engines = dict(engines or {})
        self._use_default_engines = engines is None
        self._agentic = agentic_wrapper
        self._retention_config = retention_config
        self._retention_selector = retention_selector
        if final_filter is not None:
            self._final_filter = final_filter
        else:
            self._final_filter = SearchFinalFilter(
                rerank_client=rerank_client,
                rerank_client_factory=None if rerank_client is not None else _optional_rerank_client,
            )

    async def search(self, inp: SearchPipelineInput, context: MemoryRequestContext) -> SearchPipelineResult:
        """Run search according to the request controls."""

        strategy = inp.search_pipeline
        engine = self._engine(strategy)
        if engine is None:
            available = ", ".join(sorted(self._available_engine_names()))
            raise ValueError(f"Unknown search strategy {strategy!r}. Available strategies: {available}")

        retention_active = inp.token_budget is not None
        retention_config = self._resolve_retention_config() if retention_active else None
        engine_options = (
            SearchEngineOptions(
                recall_top_k=retention_config.max_candidates,
                result_top_n=retention_config.max_candidates,
            )
            if retention_config is not None
            else None
        )
        if inp.agentic:
            candidates = await self._agentic_wrapper().run(inp, context, engine)
        else:
            candidates = await engine.search_candidates(inp, context, options=engine_options)
        ranking_candidates = (
            candidates[: retention_config.max_candidates] if retention_config is not None else candidates
        )
        filter_result = await self._final_filter.apply_with_outcome(
            query=inp.query,
            candidates=ranking_candidates,
            top_k=inp.top_k,
            rerank=inp.rerank and _strategy_allows_rerank(strategy),
            score_threshold=inp.score_threshold,
            score_output=inp.include_scores or retention_active,
            truncate=not retention_active,
        )
        filtered = filter_result.candidates
        metrics = _scoring_metrics(filtered, rerank_outcome=filter_result.rerank_outcome)
        if inp.token_budget is not None:
            assert retention_config is not None
            selector = self._retention_selector or MemoryRetentionSelector(config=retention_config)
            selection = selector.select(
                query=inp.query,
                candidates=filtered,
                token_budget=inp.token_budget,
            )
            metrics.update(
                {
                    "token_budget": inp.token_budget,
                    "candidate_count_before_retention": len(filtered),
                    "candidate_count_after_retention": len(selection.candidates),
                    "estimated_tokens_before": selection.estimated_tokens_before,
                    "estimated_tokens_after": selection.estimated_tokens_after,
                    "budget_utilization": selection.estimated_tokens_after / inp.token_budget,
                    "budget_induced_empty": selection.budget_induced_empty,
                    "retention_selector_version": retention_config.selector_version,
                    "token_estimator_version": retention_config.estimator_version,
                }
            )
            filtered = selection.candidates if inp.top_k is None else selection.candidates[: inp.top_k]
        provenance_limit = self._score_provenance_limit() if inp.include_scores else 0
        memories = [
            _project_search_result(
                candidate,
                include_scores=inp.include_scores,
                provenance_limit=provenance_limit,
            )
            for candidate in filtered
        ]
        metrics["returned_count"] = len(memories)
        return SearchPipelineResult(status="ok", memories=memories, metrics=metrics)

    def _resolve_retention_config(self) -> MemoryRetentionConfig:
        if self._retention_config is not None:
            return self._retention_config
        return get_config().algo_config.search.retention

    def _score_provenance_limit(self) -> int:
        try:
            return self._resolve_retention_config().graph_provenance_limit
        except Exception:
            return 8

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


def _project_search_result(
    candidate,
    *,
    include_scores: bool,
    provenance_limit: int,
) -> MemorySearchResultItem:
    relevance = None
    if include_scores:
        graph = []
        for evidence in candidate.evidence:
            path = evidence.graph
            if path is None:
                continue
            graph.append(
                SearchGraphProvenance(
                    seed_memory_id=path.seed_memory_id,
                    relation=path.relation,
                    hops=path.hops,
                    decay=_finite_or_none(path.decay),
                    path_score=_finite_or_none(path.path_score),
                    used_fallback=path.used_fallback,
                    entity_id=path.entity_id,
                    entity_name=path.entity_name,
                    entity_type=path.entity_type,
                )
            )
            if len(graph) >= provenance_limit:
                break
        relevance = SearchRelevance(
            score=_finite_unit(candidate.relevance_score),
            source=candidate.final_score_source,
            rank=candidate.rank,
            retrieval_score=_finite_or_none(candidate.retrieval_score),
            retrieval_score_type=candidate.retrieval_score_type,
            rerank_score=_finite_or_none(candidate.rerank_score),
            graph=graph,
        )
    return MemorySearchResultItem(**candidate.item.model_dump(), relevance=relevance)


def _finite_or_none(value: float | None) -> float | None:
    if value is None or not isfinite(value):
        return None
    return value


def _finite_unit(value: float) -> float:
    if not isfinite(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def _scoring_metrics(candidates, *, rerank_outcome: str) -> dict[str, Any]:
    source_counts: dict[str, int] = {}
    retrieval_score_type_counts: dict[str, int] = {}
    graph_source_counts: dict[str, int] = {}
    graph_path_count = 0
    for candidate in candidates:
        source = candidate.final_score_source
        source_counts[source] = source_counts.get(source, 0) + 1
        score_type = candidate.retrieval_score_type or "unavailable"
        retrieval_score_type_counts[score_type] = retrieval_score_type_counts.get(score_type, 0) + 1
        for evidence in candidate.evidence:
            if evidence.graph is None:
                continue
            graph_path_count += 1
            relation = evidence.graph.relation
            graph_source_counts[relation] = graph_source_counts.get(relation, 0) + 1
    return {
        "scoring_version": "query-local-v1",
        "candidate_count": len(candidates),
        "score_source_counts": source_counts,
        "retrieval_score_type_counts": retrieval_score_type_counts,
        "graph_path_count": graph_path_count,
        "graph_source_counts": graph_source_counts,
        "rerank_outcome": rerank_outcome,
    }
