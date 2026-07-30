"""Vanilla search engine with hybrid retrieval."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from math import isfinite

from ....components.searcher.scored_candidate import (
    GraphPathEvidence,
    RetrievalEvidence,
    ScoredSearchCandidate,
    normalize_candidate_scores,
)
from ....components.searcher.vanilla import dedup_by_text_similarity
from ....components.searcher.vanilla._executor import vanilla_dedup_executor
from ....components.text import SparseVectorEncoder, TextPreprocessor, get_text_preprocessor
from ....config import TextProcessingConfig, get_config
from ....config.algo.search import VanillaSearchConfig
from ....config.algo.search.vanilla.vanilla import (
    VANILLA_DEDUP_MAX_CANDIDATES,
    VANILLA_HYBRID_PREFETCH_MAX,
    VANILLA_RECALL_SIZE_MAX,
)
from ....llm import (
    EmbedClient,
    get_embed_client,
    provider_binding_runtime_enabled,
    require_model_endpoint,
)
from ....logging import get_logger, traced
from ....mappers import parse_search_dsl
from ....typing import (
    FieldCondition,
    MemoryDbSearchHit,
    MemoryDbSearchQuery,
    MemoryLineage,
    MemoryRequestContext,
    MemorySearchItem,
    MemoryView,
    SearchFilter,
    SearchPipelineInput,
    SparseVector,
)
from ...base import MemoryDbPipelineMixin
from ...utils import format_datetime, format_memory_event_time, format_source_timestamp
from ..base import SearchEngineOptions

logger = get_logger(__name__)


@dataclass(frozen=True)
class _GraphCandidate:
    memory_id: str
    seed_memory_id: str
    source: str
    debug: dict[str, object]


class VanillaSearchEngine(MemoryDbPipelineMixin):
    """Hybrid dense + sparse retrieval before final filtering."""

    name = "vanilla"

    def __init__(
        self,
        *,
        text_config: TextProcessingConfig | None = None,
        search_config: VanillaSearchConfig | None = None,
        text_preprocessor: TextPreprocessor | None = None,
        sparse_encoder: SparseVectorEncoder | None = None,
        embed_client: EmbedClient | None = None,
        graph_provenance_limit: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        cfg = None
        if text_config is None or embed_client is None:
            cfg = get_config()

        text_cfg = text_config or cfg.algo_config.text_processing
        self._text_preprocessor = text_preprocessor or get_text_preprocessor(text_cfg)
        self._sparse_encoder = sparse_encoder or SparseVectorEncoder(text_cfg)
        self._explicit_search_config: VanillaSearchConfig | None = search_config
        self._explicit_graph_provenance_limit = graph_provenance_limit

        self._explicit_embed_client = embed_client
        self._embed_client: EmbedClient | None = embed_client
        if self._embed_client is None and not provider_binding_runtime_enabled():
            try:
                self._embed_client = get_embed_client()
            except Exception:
                logger.debug("vanilla_search_embed_unavailable")

        logger.debug("vanilla_search_initialized", has_embed=self._embed_client is not None)

    def _get_vanilla_search_config(self) -> VanillaSearchConfig:
        if self._explicit_search_config is not None:
            return self._explicit_search_config
        return get_config().algo_config.search.vanilla

    def _get_graph_provenance_limit(self) -> int:
        if self._explicit_graph_provenance_limit is not None:
            return self._explicit_graph_provenance_limit
        try:
            return get_config().algo_config.search.retention.graph_provenance_limit
        except Exception:
            return 8

    @traced("search.vanilla")
    async def search_candidates(
        self,
        inp: SearchPipelineInput,
        context: MemoryRequestContext,
        *,
        options: SearchEngineOptions | None = None,
    ) -> list[ScoredSearchCandidate]:
        """Search memories via hybrid dense+sparse retrieval and return candidates."""

        scfg = self._get_vanilla_search_config()
        preprocessed = self._text_preprocessor.preprocess_query(inp.query, include_entities=False)
        if not preprocessed.tokens:
            return []

        dense_vector, sparse_vector = await asyncio.gather(
            self._encode_dense(inp.query),
            self._encode_sparse(preprocessed.tokens),
        )

        filters = _request_filter(inp, context)
        request_top_k = options.result_top_n if options and options.result_top_n is not None else inp.top_k
        configured_recall_size = (
            options.recall_top_k if options and options.recall_top_k is not None else scfg.recall_size
        )
        recall_size = configured_recall_size if request_top_k is None else max(configured_recall_size, request_top_k)
        recall_size = min(recall_size, VANILLA_RECALL_SIZE_MAX)

        if dense_vector is not None:
            query = MemoryDbSearchQuery(
                query=inp.query,
                top_k=recall_size,
                filters=filters,
                mode="rrf",
                ranking="hybrid",
            )
            prefetch_limit = max(
                recall_size * scfg.hybrid_prefetch_factor,
                scfg.hybrid_prefetch_min,
            )
            prefetch_limit = min(
                prefetch_limit,
                scfg.hybrid_prefetch_max,
                VANILLA_HYBRID_PREFETCH_MAX,
            )
            result = await self.db_reader.search_hybrid(
                context,
                query,
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                dense_limit=prefetch_limit,
                sparse_limit=prefetch_limit,
            )
        else:
            logger.debug("vanilla_search_sparse_fallback")
            query = MemoryDbSearchQuery(
                query=inp.query,
                top_k=recall_size,
                filters=filters,
                mode="bm25",
                ranking="score",
            )
            result = await self.db_reader.search_sparse(
                context,
                query,
                indices=list(sparse_vector.indices),
                values=list(sparse_vector.values),
            )

        hits = await self._with_graph_related_hits(result.hits, filters, context)
        ranked_hits = _rank_by_score(hits)
        lineage_by_id, derived_to_by_id, archived_hits = await self._lineage_for_existing_hits(ranked_hits, context)
        ranked_hits = [*ranked_hits, *archived_hits]
        direct_score_type = "rrf" if dense_vector is not None else "bm25"
        candidates = [
            _to_scored_search_candidate(
                hit,
                index=index,
                direct_score_type=direct_score_type,
                lineage=_lineage_for_hit(hit, lineage_by_id=lineage_by_id, derived_to_by_id=derived_to_by_id),
            )
            for index, hit in enumerate(ranked_hits)
        ]
        if scfg.dedup_enabled:
            kept_items = await vanilla_dedup_executor.run(
                dedup_by_text_similarity,
                [candidate.item for candidate in candidates],
                threshold=scfg.dedup_threshold,
                group_keys=[_dedup_group_for_hit(hit) for hit in ranked_hits],
                max_candidates=min(scfg.dedup_max_candidates, VANILLA_DEDUP_MAX_CANDIDATES),
            )
            by_id = {candidate.item.id: candidate for candidate in candidates}
            candidates = [by_id[item.id] for item in kept_items if item.id in by_id]
        for index, candidate in enumerate(candidates):
            candidate.rank = index
        return normalize_candidate_scores(candidates)

    def _resolve_embed_client(self) -> EmbedClient | None:
        if self._explicit_embed_client is not None:
            return self._explicit_embed_client
        if provider_binding_runtime_enabled():
            require_model_endpoint("embedding")
            return get_embed_client()
        return self._embed_client

    async def _encode_dense(self, query: str) -> list[float] | None:
        """Generate a dense embedding; return None when unavailable."""
        embed_client = self._resolve_embed_client()
        if embed_client is None:
            return None
        try:
            resp = await embed_client.embed(task="search.query", text=query)
            return resp.embeddings[0] if resp.embeddings else None
        except Exception:
            logger.warning("vanilla_search_dense_embed_failed", exc_info=True)
            return None

    async def _encode_sparse(self, tokens: list[str]) -> SparseVector:
        """Generate the sparse BM25 query vector."""
        return self._sparse_encoder.encode_query(tokens)

    async def _with_graph_related_hits(
        self,
        hits: list[MemoryDbSearchHit],
        filters: SearchFilter,
        context: MemoryRequestContext,
    ) -> list[MemoryDbSearchHit]:
        """Append Neo4j one-hop related memories, hydrating them through Qdrant batch read."""

        scfg = self._get_vanilla_search_config()
        if not (scfg.graph_enabled or scfg.shared_entity_graph_enabled) or not hits:
            return hits

        seed_ids = _dedupe_ids(hit.memory_id for hit in hits[: max(0, scfg.graph_seed_memory_limit)] if hit.memory_id)
        if not seed_ids:
            return hits

        existing_ids = {hit.memory_id for hit in hits}
        seed_scores = {hit.memory_id: hit.score for hit in hits}
        related_by_id: dict[str, list[_GraphCandidate]] = {}

        try:
            direct_candidates = await self._graph_candidates_by_direct_relation(
                context,
                seed_ids,
                existing_ids=existing_ids,
            )
            _merge_graph_candidate_maps(related_by_id, direct_candidates)
            max_candidates = max(0, scfg.graph_max_candidates)
            remaining_candidates = max(
                0,
                max_candidates - sum(memory_id not in existing_ids for memory_id in related_by_id),
            )
            shared_candidates = await self._graph_candidates_by_shared_entity(
                context,
                seed_ids,
                existing_ids=existing_ids | set(related_by_id),
                max_candidates=remaining_candidates,
            )
            _merge_graph_candidate_maps(related_by_id, shared_candidates)
        except Exception:
            logger.warning("vanilla_search_graph_related_failed", exc_info=True)
            return hits

        if not related_by_id:
            return hits

        hits_by_id = {hit.memory_id: hit for hit in hits}
        provenance_limit = self._get_graph_provenance_limit()
        for memory_id, graph_candidates in related_by_id.items():
            existing_hit = hits_by_id.get(memory_id)
            if existing_hit is None:
                continue
            paths = existing_hit.debug.setdefault("graph_paths", [])
            if isinstance(paths, list):
                paths.extend(
                    _ranked_graph_path_debug(
                        graph_candidates,
                        seed_scores=seed_scores,
                        decay=scfg.graph_decay,
                        fallback=scfg.graph_score,
                    )[:provenance_limit]
                )

        candidate_ids = [memory_id for memory_id in related_by_id if memory_id not in existing_ids]
        if not candidate_ids:
            return hits

        try:
            related_memories = await self.db_reader.get_memories(context, candidate_ids)
        except Exception:
            logger.warning("vanilla_search_graph_hydrate_failed", exc_info=True)
            return hits

        rank_start = len(hits) + 1
        graph_hits: list[MemoryDbSearchHit] = []
        for offset, memory in enumerate(related_memories):
            if memory.memory_id in existing_ids:
                continue
            if not _memory_matches_filter(memory, filters):
                continue
            graph_candidates = related_by_id.get(memory.memory_id) or [
                _GraphCandidate(
                    memory_id=memory.memory_id,
                    seed_memory_id="",
                    source="neo4j_graph",
                    debug={"graph_source": "unknown", "seed_memory_ids": seed_ids},
                )
            ]
            ranked_paths = _ranked_graph_path_debug(
                graph_candidates,
                seed_scores=seed_scores,
                decay=scfg.graph_decay,
                fallback=scfg.graph_score,
            )[:provenance_limit]
            primary_path = ranked_paths[0]
            graph_score = float(primary_path["graph_path_score"])
            primary_candidate = next(
                candidate
                for candidate in graph_candidates
                if candidate.seed_memory_id == primary_path["seed_memory_id"]
                and candidate.debug.get("graph_source") == primary_path["graph_source"]
            )
            graph_hits.append(
                MemoryDbSearchHit(
                    memory_id=memory.memory_id,
                    score=graph_score,
                    memory=memory,
                    source=primary_candidate.source,
                    rank=rank_start + offset,
                    debug={
                        **primary_path,
                        "graph_paths": ranked_paths[1:],
                    },
                )
            )
            existing_ids.add(memory.memory_id)

        return [*hits, *graph_hits]

    async def _lineage_for_existing_hits(
        self,
        hits: list[MemoryDbSearchHit],
        context: MemoryRequestContext,
    ) -> tuple[dict[str, list[str]], dict[str, list[str]], list[MemoryDbSearchHit]]:
        if not hits:
            return {}, {}, []

        seed_ids = _dedupe_ids(hit.memory_id for hit in hits if hit.memory_id)
        if not seed_ids:
            return {}, {}, []

        try:
            lineage_by_id = await self.db_reader.get_memory_lineage(context, seed_ids)
            archived_ids = _ordered_archived_ids(lineage_by_id, existing_ids=set(seed_ids))
            archived_memories = await self.db_reader.get_memories(context, archived_ids) if archived_ids else []
            if archived_ids:
                archived_lineage = await self.db_reader.get_memory_lineage(context, archived_ids)
                lineage_by_id.update(archived_lineage)
        except Exception:
            logger.warning("vanilla_search_archived_memory_hydrate_failed", exc_info=True)
            return {}, {}, []

        archived_memories = _sort_memories_by_time_desc(_ordered_memories(archived_memories, archived_ids))
        lineage_by_id = _sort_lineage_ids_by_memory_time(lineage_by_id, archived_memories)
        archived_hits = [
            MemoryDbSearchHit(
                memory_id=memory.memory_id,
                score=0.0,
                memory=memory,
                source="lineage_archived",
                rank=len(hits) + index,
            )
            for index, memory in enumerate(archived_memories)
        ]
        return lineage_by_id, _derived_to_by_id(lineage_by_id, seed_ids), archived_hits

    async def _graph_candidates_by_direct_relation(
        self,
        context: MemoryRequestContext,
        seed_ids: list[str],
        *,
        existing_ids: set[str],
    ) -> dict[str, list[_GraphCandidate]]:
        scfg = self._get_vanilla_search_config()
        if not scfg.graph_enabled:
            return {}
        related = await self.db_reader.get_related_memory_ids(
            context,
            seed_ids,
            limit_per_memory=max(0, scfg.graph_related_per_seed),
            max_candidates=max(0, scfg.graph_max_candidates),
        )
        candidates: dict[str, list[_GraphCandidate]] = {}
        for item in _normalize_related_items(related):
            memory_id = item["memory_id"]
            seed_memory_id = item["seed_memory_id"]
            if memory_id == seed_memory_id:
                continue
            _append_graph_candidate(
                candidates,
                _GraphCandidate(
                    memory_id=memory_id,
                    seed_memory_id=seed_memory_id,
                    source="neo4j_relates_to",
                    debug={
                        "graph_source": "relates_to",
                        "seed_memory_id": seed_memory_id,
                        "seed_memory_ids": seed_ids,
                    },
                ),
            )
        return candidates

    async def _graph_candidates_by_shared_entity(
        self,
        context: MemoryRequestContext,
        seed_ids: list[str],
        *,
        existing_ids: set[str],
        max_candidates: int,
    ) -> dict[str, list[_GraphCandidate]]:
        scfg = self._get_vanilla_search_config()
        if not scfg.shared_entity_graph_enabled:
            return {}
        scopes = await self.db_reader.list_memories_by_shared_entities(
            context,
            seed_ids,
            include_seed=False,
            active_only=True,
            limit_per_entity=max(0, scfg.shared_entity_graph_limit_per_entity),
        )

        candidates: dict[str, list[_GraphCandidate]] = {}
        new_candidate_count = 0
        for scope in scopes:
            seed_memory_id = str(scope.seed_memory_id or "")
            for memory_id in _dedupe_ids(scope.memory_ids):
                if memory_id == seed_memory_id:
                    continue
                is_new_memory = memory_id not in existing_ids and memory_id not in candidates
                if is_new_memory and (max_candidates <= 0 or new_candidate_count >= max_candidates):
                    continue
                _append_graph_candidate(
                    candidates,
                    _GraphCandidate(
                        memory_id=memory_id,
                        seed_memory_id=seed_memory_id,
                        source="neo4j_shared_entity",
                        debug={
                            "graph_source": "shared_entity",
                            "seed_memory_id": seed_memory_id,
                            "seed_memory_ids": seed_ids,
                            "entity_id": scope.entity_id,
                            "entity_name": scope.entity_name,
                            "entity_type": scope.entity_type,
                        },
                    ),
                )
                if is_new_memory:
                    new_candidate_count += 1
        return candidates


def _request_filter(inp: SearchPipelineInput, ctx: MemoryRequestContext) -> SearchFilter:
    """Combine user DSL with the always-on active-memory scope."""
    base = parse_search_dsl(inp.filters)
    defaults = _default_scope(ctx)
    return SearchFilter(
        must=[*defaults, *base.must],
        should=base.should,
        must_not=base.must_not,
    )


def _default_scope(ctx: MemoryRequestContext) -> list[FieldCondition]:
    """Build always-on scope conditions for public search pipelines."""
    return [FieldCondition(field="status", op="match", value="active")]


def _dedupe_ids(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_related_items(values) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            memory_id = str(value.get("memory_id") or "")
            seed_memory_id = str(value.get("seed_memory_id") or "")
        else:
            memory_id = str(value or "")
            seed_memory_id = ""
        if not memory_id:
            continue
        result.append({"memory_id": memory_id, "seed_memory_id": seed_memory_id})
    return result


def _append_graph_candidate(
    candidates: dict[str, list[_GraphCandidate]],
    candidate: _GraphCandidate,
) -> None:
    paths = candidates.setdefault(candidate.memory_id, [])
    key = (
        candidate.seed_memory_id,
        candidate.source,
        candidate.debug.get("entity_id"),
    )
    if any((path.seed_memory_id, path.source, path.debug.get("entity_id")) == key for path in paths):
        return
    paths.append(candidate)


def _merge_graph_candidate_maps(
    target: dict[str, list[_GraphCandidate]],
    incoming: dict[str, list[_GraphCandidate]],
) -> None:
    for paths in incoming.values():
        for path in paths:
            _append_graph_candidate(target, path)


def _ranked_graph_path_debug(
    candidates: list[_GraphCandidate],
    *,
    seed_scores: dict[str, float],
    decay: float,
    fallback: float,
) -> list[dict[str, object]]:
    paths = []
    for candidate in candidates:
        seed_score = seed_scores.get(candidate.seed_memory_id)
        paths.append(
            {
                **candidate.debug,
                "graph_decay": decay,
                "graph_path_score": _graph_score(seed_score, decay=decay, fallback=fallback),
                "graph_score_fallback": _graph_score_uses_fallback(seed_score),
            }
        )
    return sorted(
        paths,
        key=lambda path: (
            -float(path["graph_path_score"]),
            str(path.get("seed_memory_id") or ""),
            str(path.get("graph_source") or ""),
            str(path.get("entity_id") or ""),
        ),
    )


def _graph_score(seed_score: float | None, *, decay: float, fallback: float) -> float:
    safe_fallback = fallback if isfinite(fallback) and fallback >= 0 else 0.0
    if _graph_score_uses_fallback(seed_score):
        return safe_fallback
    safe_decay = decay if isfinite(decay) and decay >= 0 else 0.0
    score = float(seed_score) * safe_decay
    return score if isfinite(score) and score >= 0 else safe_fallback


def _graph_score_uses_fallback(seed_score: float | None) -> bool:
    return seed_score is None or not isfinite(seed_score) or seed_score < 0


def _rank_by_score(hits: list[MemoryDbSearchHit]) -> list[MemoryDbSearchHit]:
    return [
        hit
        for _, hit in sorted(
            enumerate(hits),
            key=lambda item: (
                -(item[1].score or 0.0),
                item[1].rank if item[1].rank is not None else item[0],
                item[0],
            ),
        )
    ]


def _lineage_for_hit(
    hit: MemoryDbSearchHit,
    *,
    lineage_by_id: dict[str, list[str]],
    derived_to_by_id: dict[str, list[str]],
) -> MemoryLineage:
    role = "archived" if hit.source == "lineage_archived" else "current"
    return MemoryLineage(
        role=role,
        derived_from_memory_ids=lineage_by_id.get(hit.memory_id, []),
        derived_to_memory_ids=derived_to_by_id.get(hit.memory_id, []),
    )


def _dedup_group_for_hit(hit: MemoryDbSearchHit) -> tuple[str | None, str]:
    memory = hit.memory
    lineage_role = "archived" if hit.source == "lineage_archived" else "current"
    return (memory.user_id if memory else None, lineage_role)


def _to_memory_search_item(hit: MemoryDbSearchHit, *, lineage: MemoryLineage | None = None) -> MemorySearchItem:
    memory = hit.memory
    return MemorySearchItem(
        id=hit.memory_id,
        memory=memory.content if memory else "",
        memory_type=memory.mem_type if memory else "fact",
        last_update_at=format_datetime((memory.update_at or memory.created_at) if memory else None),
        event_time=format_memory_event_time(memory, fallback_to_source_timestamp=True) if memory else None,
        source_timestamp=format_source_timestamp(memory) if memory else None,
        lineage=lineage,
    )


def _to_scored_search_candidate(
    hit: MemoryDbSearchHit,
    *,
    index: int,
    direct_score_type: str,
    lineage: MemoryLineage | None = None,
) -> ScoredSearchCandidate:
    is_graph = str(hit.source or "").startswith("neo4j")
    is_lineage = hit.source == "lineage_archived"
    if is_lineage:
        return ScoredSearchCandidate(
            item=_to_memory_search_item(hit, lineage=lineage),
            original_rank=hit.rank if hit.rank is not None else index,
            rank=index,
            evidence=[RetrievalEvidence(source="lineage", rank=index)],
        )
    score_type = "graph_propagation" if is_graph else direct_score_type
    graph = _graph_path_evidence(hit) if is_graph else None
    evidence = [
        RetrievalEvidence(
            source="graph" if is_graph else "direct",
            score=hit.score,
            score_type=score_type,
            rank=hit.rank if hit.rank is not None else index,
            graph=graph,
        )
    ]
    evidence.extend(_merged_graph_evidence(hit))
    return ScoredSearchCandidate(
        item=_to_memory_search_item(hit, lineage=lineage),
        original_rank=hit.rank if hit.rank is not None else index,
        rank=index,
        retrieval_score=hit.score,
        retrieval_score_type=score_type,
        evidence=evidence,
    )


def _graph_path_evidence(hit: MemoryDbSearchHit) -> GraphPathEvidence:
    debug = hit.debug or {}
    return GraphPathEvidence(
        seed_memory_id=str(debug.get("seed_memory_id") or ""),
        relation=str(debug.get("graph_source") or "unknown"),
        hops=1,
        decay=_optional_float(debug.get("graph_decay")),
        path_score=_optional_float(debug.get("graph_path_score")) or hit.score,
        used_fallback=bool(debug.get("graph_score_fallback", False)),
        entity_id=_optional_text(debug.get("entity_id")),
        entity_name=_optional_text(debug.get("entity_name")),
        entity_type=_optional_text(debug.get("entity_type")),
    )


def _merged_graph_evidence(hit: MemoryDbSearchHit) -> list[RetrievalEvidence]:
    paths = hit.debug.get("graph_paths", []) if hit.debug else []
    if not isinstance(paths, list):
        return []
    evidence: list[RetrievalEvidence] = []
    for path in paths:
        if not isinstance(path, dict):
            continue
        path_score = _optional_float(path.get("graph_path_score"))
        evidence.append(
            RetrievalEvidence(
                source="graph",
                score=path_score,
                score_type="graph_propagation",
                graph=GraphPathEvidence(
                    seed_memory_id=str(path.get("seed_memory_id") or ""),
                    relation=str(path.get("graph_source") or "unknown"),
                    hops=1,
                    decay=_optional_float(path.get("graph_decay")),
                    path_score=path_score,
                    used_fallback=bool(path.get("graph_score_fallback", False)),
                    entity_id=_optional_text(path.get("entity_id")),
                    entity_name=_optional_text(path.get("entity_name")),
                    entity_type=_optional_text(path.get("entity_type")),
                ),
            )
        )
    return evidence


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _ordered_archived_ids(lineage_by_id: dict[str, list[str]], *, existing_ids: set[str]) -> list[str]:
    seen = set(existing_ids)
    result: list[str] = []
    for derived_from_ids in lineage_by_id.values():
        for memory_id in derived_from_ids:
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            result.append(memory_id)
    return result


def _ordered_memories(memories: list[MemoryView], memory_ids: list[str]) -> list[MemoryView]:
    by_id = {memory.memory_id: memory for memory in memories}
    return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]


def _sort_memories_by_time_desc(memories: list[MemoryView]) -> list[MemoryView]:
    return sorted(memories, key=lambda memory: (-_memory_time(memory), memory.memory_id))


def _sort_lineage_ids_by_memory_time(
    lineage_by_id: dict[str, list[str]],
    memories: list[MemoryView],
) -> dict[str, list[str]]:
    order = {memory.memory_id: index for index, memory in enumerate(memories)}
    result: dict[str, list[str]] = {}
    for memory_id, derived_from_ids in lineage_by_id.items():
        result[memory_id] = sorted(
            derived_from_ids,
            key=lambda derived_id: (
                order.get(derived_id, len(order)),
                derived_from_ids.index(derived_id),
            ),
        )
    return result


def _memory_time(memory: MemoryView) -> float:
    value = memory.update_at or memory.created_at
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _derived_to_by_id(lineage_by_id: dict[str, list[str]], current_ids: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for current_id in current_ids:
        for memory_id in lineage_by_id.get(current_id, []):
            result.setdefault(memory_id, []).append(current_id)
    return result


def _memory_matches_filter(memory: MemoryView, filters: SearchFilter) -> bool:
    return (
        all(_matches_clause(memory, clause) for clause in filters.must)
        and (not filters.should or any(_matches_clause(memory, clause) for clause in filters.should))
        and not any(_matches_clause(memory, clause) for clause in filters.must_not)
    )


def _matches_clause(memory: MemoryView, clause: FieldCondition | SearchFilter) -> bool:
    if isinstance(clause, SearchFilter):
        return _memory_matches_filter(memory, clause)
    value = _memory_field_value(memory, clause.field)
    match clause.op:
        case "match":
            return value == clause.value
        case "any":
            values = clause.values or []
            if isinstance(value, list):
                return any(item in values for item in value)
            return value in values
        case "except":
            values = clause.values or []
            if isinstance(value, list):
                return all(item not in values for item in value)
            return value not in values
        case "text":
            if value is None or clause.value is None:
                return False
            return str(clause.value).lower() in str(value).lower()
        case "range" | "datetime":
            return _value_in_range(value, clause)
        case "is_empty":
            return _is_empty_value(value)
        case "is_null":
            return value is None
    return False


def _memory_field_value(memory: MemoryView, field: str):
    if hasattr(memory, field):
        return getattr(memory, field)
    return memory.metadata.get(field)


def _value_in_range(value, condition: FieldCondition) -> bool:
    if value is None:
        return False
    comparable = value
    for attr, predicate in (
        ("gt", lambda a, b: a > b),
        ("gte", lambda a, b: a >= b),
        ("lt", lambda a, b: a < b),
        ("lte", lambda a, b: a <= b),
    ):
        bound = getattr(condition, attr)
        try:
            if bound is not None and not predicate(comparable, bound):
                return False
        except TypeError:
            return False
    return True


def _is_empty_value(value) -> bool:
    return value is None or value == "" or value == [] or value == {}
