import json
from datetime import UTC, datetime
from math import ceil, log2
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from mindmemos.components.searcher.memory_retention import MemoryRetentionSelector
from mindmemos.components.searcher.scored_candidate import (
    GraphPathEvidence,
    RetrievalEvidence,
    ScoredSearchCandidate,
)
from mindmemos.components.text.token_estimator import estimate_tokens
from mindmemos.config.algo.search.retention import MemoryRetentionConfig
from mindmemos.typing.service import MemorySearchItem


class BenchmarkTextPreprocessor:
    def preprocess_query(self, text: str, *, include_entities: bool = False):
        return SimpleNamespace(tokens=text.casefold().split())

    def preprocess_text(self, text: str, *, include_entities: bool = False):
        return SimpleNamespace(tokens=text.casefold().split())


def test_mixed_retention_benchmark_preserves_evidence_under_strict_budgets() -> None:
    cases = json.loads(
        (Path(__file__).parents[2] / "fixtures" / "search_retention_benchmark.json").read_text(encoding="utf-8")
    )
    selector = MemoryRetentionSelector(
        config=MemoryRetentionConfig(),
        text_preprocessor=BenchmarkTextPreprocessor(),
        now=lambda: datetime(2026, 1, 31, tzinfo=UTC),
    )
    rank_only_selector = MemoryRetentionSelector(
        config=MemoryRetentionConfig(
            relevance_weight=1.0,
            query_overlap_weight=0.0,
            recency_weight=0.0,
            cost_weight=0.0,
        ),
        text_preprocessor=BenchmarkTextPreprocessor(),
        now=lambda: datetime(2026, 1, 31, tzinfo=UTC),
    )
    measurements = {
        "baseline": _empty_measurements(),
        "rank_only": _empty_measurements(),
        "mixed": _empty_measurements(),
    }
    selector_latency_seconds = []

    for case in cases:
        candidates = [_candidate(row, tag=case.get("tag")) for row in case["candidates"]]
        relevant_ids = {row["id"] for row in case["candidates"] if row["relevant"]}
        started_at = perf_counter()
        selected = selector.select(
            query=case["query"],
            candidates=candidates,
            token_budget=case["budget"],
        )
        selector_latency_seconds.append(perf_counter() - started_at)
        rank_only = rank_only_selector.select(
            query=case["query"],
            candidates=candidates,
            token_budget=case["budget"],
        )
        assert selected.estimated_tokens_after <= case["budget"], case["name"]
        _record(measurements["baseline"], candidates, relevant_ids, provider_candidate_count=len(candidates))
        _record(
            measurements["rank_only"],
            rank_only.candidates,
            relevant_ids,
            provider_candidate_count=len(candidates),
        )
        _record(
            measurements["mixed"],
            selected.candidates,
            relevant_ids,
            provider_candidate_count=len(candidates),
        )

    baseline = _summarize(measurements["baseline"], len(cases))
    rank_only = _summarize(measurements["rank_only"], len(cases))
    mixed = _summarize(measurements["mixed"], len(cases))

    assert mixed["evidence_recall"] >= 0.875
    assert mixed["evidence_recall"] >= rank_only["evidence_recall"]
    assert mixed["mrr"] >= rank_only["mrr"]
    assert mixed["ndcg"] >= rank_only["ndcg"]
    # Fixture-level answerability is the deterministic proxy available without
    # invoking a downstream answer model: at least one labelled evidence item remains.
    assert mixed["answerable_rate"] >= rank_only["answerable_rate"]
    assert mixed["empty_rate"] == 0.0
    assert mixed["average_tokens"] < baseline["average_tokens"]
    assert mixed["p95_tokens"] < baseline["p95_tokens"]
    # Retention executes after reranking and therefore does not increase provider
    # candidate cost; both selectors start from the same bounded candidate pools.
    assert measurements["mixed"]["provider_candidates"] == measurements["baseline"]["provider_candidates"]
    assert measurements["rank_only"]["provider_candidates"] == measurements["baseline"]["provider_candidates"]
    assert all(latency >= 0.0 for latency in selector_latency_seconds)


def _empty_measurements() -> dict:
    return {
        "relevant_total": 0,
        "relevant_retained": 0,
        "reciprocal_ranks": [],
        "ndcg": [],
        "answerable": 0,
        "empty": 0,
        "tokens": [],
        "provider_candidates": [],
    }


def _record(
    measurements: dict,
    candidates: list[ScoredSearchCandidate],
    relevant_ids: set[str],
    *,
    provider_candidate_count: int,
) -> None:
    candidate_ids = [candidate.id for candidate in candidates]
    relevant_ranks = [index + 1 for index, candidate_id in enumerate(candidate_ids) if candidate_id in relevant_ids]
    measurements["relevant_total"] += len(relevant_ids)
    measurements["relevant_retained"] += len(relevant_ranks)
    measurements["reciprocal_ranks"].append(1.0 / relevant_ranks[0] if relevant_ranks else 0.0)
    dcg = sum(1.0 / log2(rank + 1) for rank in relevant_ranks)
    ideal_count = min(len(relevant_ids), len(candidates))
    ideal_dcg = sum(1.0 / log2(rank + 1) for rank in range(1, ideal_count + 1))
    measurements["ndcg"].append(dcg / ideal_dcg if ideal_dcg else 0.0)
    measurements["answerable"] += bool(relevant_ranks)
    measurements["empty"] += not candidates
    measurements["tokens"].append(sum(estimate_tokens(candidate.memory) for candidate in candidates))
    measurements["provider_candidates"].append(provider_candidate_count)


def _summarize(measurements: dict, query_count: int) -> dict[str, float]:
    token_counts = sorted(measurements["tokens"])
    p95_index = max(ceil(0.95 * len(token_counts)) - 1, 0)
    return {
        "evidence_recall": measurements["relevant_retained"] / measurements["relevant_total"],
        "mrr": sum(measurements["reciprocal_ranks"]) / query_count,
        "ndcg": sum(measurements["ndcg"]) / query_count,
        "answerable_rate": measurements["answerable"] / query_count,
        "empty_rate": measurements["empty"] / query_count,
        "average_tokens": sum(token_counts) / query_count,
        "p95_tokens": token_counts[p95_index],
    }


def _candidate(row: dict, *, tag: str | None) -> ScoredSearchCandidate:
    evidence = []
    if tag == "graph":
        evidence.append(
            RetrievalEvidence(
                source="graph",
                score=row["relevance"],
                score_type="graph_propagation",
                graph=GraphPathEvidence(
                    seed_memory_id="benchmark-seed",
                    relation="relates_to",
                    decay=0.5,
                    path_score=row["relevance"],
                ),
            )
        )
    return ScoredSearchCandidate(
        item=MemorySearchItem(
            id=row["id"],
            memory=row["text"],
            last_update_at=row["event_time"],
            event_time=row["event_time"],
        ),
        original_rank=row["rank"],
        rank=row["rank"],
        relevance_score=row["relevance"],
        final_score_source="rerank" if tag == "reranked" else "rank_fallback",
        evidence=evidence,
    )
