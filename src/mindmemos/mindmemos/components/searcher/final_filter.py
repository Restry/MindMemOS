"""Final search result filtering shared by all search pipelines."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Awaitable, Literal

from ...llm import RerankClient
from ...logging import get_logger
from ...typing import MemorySearchItem
from .rerank import rerank as rerank_documents
from .rerank import rerank_with_scores as rerank_documents_with_scores
from .scored_candidate import ScoredSearchCandidate, normalize_candidate_scores, normalize_external_scores

logger = get_logger(__name__)
RerankClientFactory = Callable[[], RerankClient | None]
RerankOutcome = Literal["not_requested", "succeeded", "fallback"]


@dataclass(frozen=True, slots=True)
class SearchFinalFilterResult:
    """Candidates plus request-local reranker execution outcome."""

    candidates: list[ScoredSearchCandidate]
    rerank_outcome: RerankOutcome


class SearchFinalFilter:
    """Apply final rerank and top-k truncation to search candidates."""

    def __init__(
        self,
        rerank_client: RerankClient | None = None,
        rerank_client_factory: RerankClientFactory | None = None,
        rerank_fn: Callable[[RerankClient, str, list[str], int], Awaitable[list[int]]] = rerank_documents,
        rerank_with_scores_fn: Callable[
            [RerankClient, str, list[str], int], Awaitable[list[tuple[int, float]]]
        ] = rerank_documents_with_scores,
    ) -> None:
        self._rerank_client = rerank_client
        self._rerank_client_factory = rerank_client_factory
        self._rerank_fn = rerank_fn
        self._rerank_with_scores_fn = rerank_with_scores_fn

    async def apply(
        self,
        *,
        query: str,
        candidates: list[MemorySearchItem] | list[ScoredSearchCandidate],
        top_k: int | None,
        rerank: bool,
        score_threshold: float | None = None,
        score_output: bool = False,
        truncate: bool = True,
    ) -> list[ScoredSearchCandidate]:
        """Return final search results after optional rerank and truncation."""

        result = await self.apply_with_outcome(
            query=query,
            candidates=candidates,
            top_k=top_k,
            rerank=rerank,
            score_threshold=score_threshold,
            score_output=score_output,
            truncate=truncate,
        )
        return result.candidates

    async def apply_with_outcome(
        self,
        *,
        query: str,
        candidates: list[MemorySearchItem] | list[ScoredSearchCandidate],
        top_k: int | None,
        rerank: bool,
        score_threshold: float | None = None,
        score_output: bool = False,
        truncate: bool = True,
    ) -> SearchFinalFilterResult:
        """Return final candidates and an explicit request-local rerank outcome."""

        if not candidates:
            return SearchFinalFilterResult([], "fallback" if rerank else "not_requested")

        scored_candidates = _as_scored_candidates(candidates)
        result = scored_candidates
        rerank_outcome: RerankOutcome = "not_requested"
        if rerank:
            rerank_client = self._ensure_rerank_client()
            if (
                rerank_client is None
                or not rerank_client.available
                or not getattr(rerank_client, "has_external_model", True)
            ):
                logger.debug("search_final_rerank_unavailable")
                fallback = _truncate(scored_candidates, top_k) if truncate else scored_candidates
                return SearchFinalFilterResult(fallback, "fallback")
            documents = [candidate.item.memory for candidate in scored_candidates]
            limit = len(scored_candidates) if top_k is None or not truncate else min(top_k, len(scored_candidates))
            try:
                if score_threshold is not None or score_output:
                    scored = await self._rerank_with_scores_fn(rerank_client, query, documents, limit)
                    valid_scored = [
                        (idx, score)
                        for idx, score in scored
                        if 0 <= idx < len(scored_candidates)
                        and isfinite(score)
                        and (score_threshold is None or score >= score_threshold)
                    ]
                    normalized = normalize_external_scores([score for _, score in valid_scored])
                    result = []
                    for rank, ((idx, score), normalized_score) in enumerate(zip(valid_scored, normalized, strict=True)):
                        candidate = scored_candidates[idx]
                        candidate.rank = rank
                        candidate.rerank_score = score
                        candidate.normalized_rerank_score = normalized_score
                        candidate.relevance_score = normalized_score
                        candidate.final_score_source = "rerank"
                        result.append(candidate)
                else:
                    indices = await self._rerank_fn(rerank_client, query, documents, limit)
                    result = [scored_candidates[i] for i in indices if 0 <= i < len(scored_candidates)]
                    for rank, candidate in enumerate(result):
                        candidate.rank = rank
                rerank_outcome = "succeeded"
            except Exception:
                logger.warning("search_final_rerank_failed", exc_info=True)
                fallback = _truncate(scored_candidates, top_k) if truncate else scored_candidates
                return SearchFinalFilterResult(fallback, "fallback")

        filtered = _truncate(result, top_k) if truncate else result
        return SearchFinalFilterResult(filtered, rerank_outcome)

    def _ensure_rerank_client(self) -> RerankClient | None:
        # Never cache the factory result: this filter is held by a process-wide
        # singleton (SearchPipelineImpl._final_filter), and rerank clients are
        # project-scoped (resolved per request via get_config()). Caching would pin
        # the first project's client for all subsequent projects. An explicitly
        # injected client (tests) still wins and is returned as-is.
        if self._rerank_client is not None:
            return self._rerank_client
        if self._rerank_client_factory is not None:
            return self._rerank_client_factory()
        return None


def _truncate(candidates: list[ScoredSearchCandidate], top_k: int | None) -> list[ScoredSearchCandidate]:
    if top_k is None:
        return candidates
    return candidates[:top_k]


def _as_scored_candidates(
    candidates: list[MemorySearchItem] | list[ScoredSearchCandidate],
) -> list[ScoredSearchCandidate]:
    if not candidates:
        return []
    if isinstance(candidates[0], ScoredSearchCandidate):
        return list(candidates)  # type: ignore[arg-type]
    return normalize_candidate_scores(
        [ScoredSearchCandidate(item=item, original_rank=index, rank=index) for index, item in enumerate(candidates)]
    )
