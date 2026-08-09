"""Final search result filtering shared by all search pipelines."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable

from ...llm import RerankClient
from ...logging import get_logger
from ...typing import MemorySearchItem
from .rerank import rerank as rerank_documents
from .rerank import rerank_with_scores as rerank_documents_with_scores

logger = get_logger(__name__)
RerankClientFactory = Callable[[], RerankClient | None]
_CURRENT_STATE_RE = re.compile(
    r"(?:现在|当前|目前|最新|现状|状态|进度|是否已经|还在|仍然|如今|"
    r"\b(?:current|currently|latest|now|status|progress|still)\b)",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(r"(?:对比|比较|区别|差异|哪个更|\b(?:compare|versus|vs\.?|difference)\b)", re.I)
_LIST_RE = re.compile(r"(?:有哪些|列出|全部|清单|列表|\b(?:list|all|which)\b)", re.I)
_HISTORY_RE = re.compile(r"(?:以前|之前|当时|历史|曾经|何时|什么时候|\b(?:before|previous|history|when)\b)", re.I)
_WORD_RE = re.compile(r"[a-z0-9_.:/+-]{2,}", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")


def classify_query_intent(query: str) -> str:
    """Classify the small intent subset that changes retrieval shape."""

    text = query or ""
    if _CURRENT_STATE_RE.search(text):
        return "current_state"
    if _COMPARISON_RE.search(text):
        return "comparison"
    if _LIST_RE.search(text):
        return "list"
    if _HISTORY_RE.search(text):
        return "temporal_history"
    return "fact"


def _terms(text: str) -> set[str]:
    lowered = (text or "").casefold()
    terms = {match.group() for match in _WORD_RE.finditer(lowered)}
    for run in _CJK_RE.findall(lowered):
        if len(run) <= 2:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _direct_answer_score(query: str, content: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    memory_terms = _terms(content)
    return len(query_terms & memory_terms) / len(query_terms)


def _timestamp(item: MemorySearchItem) -> float:
    for value in (item.event_time, item.source_timestamp, item.last_update_at):
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _rank_direct_answers(
    query: str,
    candidates: list[MemorySearchItem],
    *,
    intent: str,
) -> list[MemorySearchItem]:
    indexed = list(enumerate(candidates))

    def key(entry: tuple[int, MemorySearchItem]) -> tuple[float, float, float, int]:
        index, item = entry
        direct = _direct_answer_score(query, item.memory)
        direct_bucket = 1.0 if direct >= 0.5 else 0.0
        recency = _timestamp(item) if intent == "current_state" else 0.0
        return (-direct_bucket, -direct, -recency, index)

    return [item for _, item in sorted(indexed, key=key)]


def _dedup_by_memory_id(candidates: list[MemorySearchItem]) -> tuple[list[MemorySearchItem], list[str]]:
    kept: list[MemorySearchItem] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for item in candidates:
        if item.id in seen:
            dropped.append(item.id)
            continue
        seen.add(item.id)
        kept.append(item)
    return kept, dropped


class SearchFinalFilter:
    """Apply lifecycle filtering, deduplication, rerank and answer-first ordering."""

    def __init__(
        self,
        rerank_client: RerankClient | None = None,
        rerank_client_factory: RerankClientFactory | None = None,
        rerank_fn: Callable[[RerankClient, str, list[str], int], Awaitable[list[int]]] = rerank_documents,
        rerank_with_scores_fn: Callable[
            [RerankClient, str, list[str], int], Awaitable[list[tuple[int, float]]]
        ] = rerank_documents_with_scores,
        rerank_timeout_seconds: float = 3.0,
    ) -> None:
        self._rerank_client = rerank_client
        self._rerank_client_factory = rerank_client_factory
        self._rerank_fn = rerank_fn
        self._rerank_with_scores_fn = rerank_with_scores_fn
        self._rerank_timeout_seconds = max(0.01, float(rerank_timeout_seconds))

    async def apply(
        self,
        *,
        query: str,
        candidates: list[MemorySearchItem],
        top_k: int | None,
        rerank: bool,
        score_threshold: float | None = None,
        quality_trace: dict[str, Any] | None = None,
    ) -> list[MemorySearchItem]:
        """Return final search results and optionally populate an audit trace."""

        started = perf_counter()
        trace = quality_trace if quality_trace is not None else {}
        trace.clear()
        trace.update({"intent": classify_query_intent(query), "candidate_ids": [item.id for item in candidates]})
        filtered: list[dict[str, str]] = []
        if not candidates:
            trace.update({"filtered": filtered, "final_ids": [], "elapsed_ms": 0.0})
            return []

        result = list(candidates)
        if trace["intent"] == "current_state":
            active: list[MemorySearchItem] = []
            for item in result:
                if item.lineage is not None and item.lineage.role == "archived":
                    filtered.append({"id": item.id, "reason": "superseded_or_archived"})
                else:
                    active.append(item)
            result = active

        result, duplicate_ids = _dedup_by_memory_id(result)
        filtered.extend({"id": memory_id, "reason": "duplicate_memory_id"} for memory_id in duplicate_ids)
        if rerank and result:
            rerank_client = self._ensure_rerank_client()
            if (
                rerank_client is None
                or not rerank_client.available
                or not getattr(rerank_client, "has_external_model", True)
            ):
                trace["degraded_reason"] = "rerank_unavailable"
            else:
                documents = [item.memory for item in result]
                limit = len(result) if top_k is None else min(top_k, len(result))
                try:
                    if score_threshold is not None:
                        scored = await asyncio.wait_for(
                            self._rerank_with_scores_fn(rerank_client, query, documents, limit),
                            timeout=self._rerank_timeout_seconds,
                        )
                        selected = [idx for idx, score in scored if score >= score_threshold]
                        below = [idx for idx, score in scored if score < score_threshold]
                        filtered.extend(
                            {"id": result[idx].id, "reason": "below_score_threshold"}
                            for idx in below
                            if 0 <= idx < len(result)
                        )
                    else:
                        selected = await asyncio.wait_for(
                            self._rerank_fn(rerank_client, query, documents, limit),
                            timeout=self._rerank_timeout_seconds,
                        )
                    result = [result[index] for index in selected if 0 <= index < len(result)]
                except TimeoutError:
                    trace["degraded_reason"] = "rerank_timeout"
                    logger.warning("search_final_rerank_timeout", timeout_seconds=self._rerank_timeout_seconds)
                except Exception:
                    trace["degraded_reason"] = "rerank_failed"
                    logger.warning("search_final_rerank_failed", exc_info=True)

        result = _rank_direct_answers(query, result, intent=str(trace["intent"]))
        if top_k is not None and len(result) > top_k:
            filtered.extend({"id": item.id, "reason": "top_k_budget"} for item in result[top_k:])
            result = result[:top_k]

        trace.update(
            {
                "filtered": filtered,
                "final_ids": [item.id for item in result],
                "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            }
        )
        logger.info("search_quality_trace", **trace)
        return result

    def _ensure_rerank_client(self) -> RerankClient | None:
        if self._rerank_client is not None:
            return self._rerank_client
        if self._rerank_client_factory is not None:
            return self._rerank_client_factory()
        return None
