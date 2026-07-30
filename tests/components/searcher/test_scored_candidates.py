import math

from mindmemos.components.searcher.scored_candidate import (
    GraphPathEvidence,
    RetrievalEvidence,
    ScoredSearchCandidate,
    merge_scored_candidates,
    normalize_candidate_scores,
)
from mindmemos.typing.service import MemorySearchItem


def _item(memory_id: str) -> MemorySearchItem:
    return MemorySearchItem(id=memory_id, memory=memory_id, last_update_at="")


def _candidate(memory_id: str, rank: int, raw_score: float | None) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        item=_item(memory_id),
        original_rank=rank,
        rank=rank,
        retrieval_score=raw_score,
        retrieval_score_type="bm25" if raw_score is not None else None,
    )


def test_normalize_candidate_scores_preserves_distinct_raw_order() -> None:
    candidates = [_candidate("a", 0, 9.0), _candidate("b", 1, 5.0), _candidate("c", 2, 1.0)]

    normalized = normalize_candidate_scores(candidates)

    assert [candidate.relevance_score for candidate in normalized] == [1.0, 0.5, 0.0]
    assert [candidate.final_score_source for candidate in normalized] == ["retrieval"] * 3
    assert [candidate.item.id for candidate in normalized] == ["a", "b", "c"]


def test_normalize_candidate_scores_uses_rank_fallback_for_equal_or_invalid_scores() -> None:
    candidates = [
        _candidate("a", 0, 1.0),
        _candidate("b", 1, 1.0),
        _candidate("c", 2, math.nan),
    ]

    normalized = normalize_candidate_scores(candidates)

    assert [candidate.relevance_score for candidate in normalized] == [1.0, 0.5, 0.0]
    assert [candidate.final_score_source for candidate in normalized] == ["rank_fallback"] * 3


def test_normalize_single_candidate_assigns_full_relevance() -> None:
    normalized = normalize_candidate_scores([_candidate("a", 0, 0.25)])

    assert normalized[0].relevance_score == 1.0
    assert normalized[0].final_score_source == "retrieval"


def test_merge_scored_candidates_keeps_direct_score_and_bounded_graph_paths() -> None:
    direct = _candidate("memory", 0, 0.8)
    direct.evidence.append(RetrievalEvidence(source="direct", score=0.8, score_type="rrf"))
    graph = ScoredSearchCandidate(
        item=_item("memory"),
        original_rank=4,
        rank=4,
        retrieval_score=0.4,
        retrieval_score_type="graph_propagation",
        evidence=[
            RetrievalEvidence(
                source="graph",
                score=0.4,
                score_type="graph_propagation",
                graph=GraphPathEvidence(
                    seed_memory_id="seed-b",
                    relation="shared_entity",
                    hops=1,
                    decay=0.5,
                    path_score=0.4,
                ),
            ),
            RetrievalEvidence(
                source="graph",
                score=0.3,
                score_type="graph_propagation",
                graph=GraphPathEvidence(
                    seed_memory_id="seed-c",
                    relation="relates_to",
                    hops=1,
                    decay=0.5,
                    path_score=0.3,
                ),
            ),
        ],
    )

    merged = merge_scored_candidates([direct, graph], evidence_limit=2)

    assert len(merged) == 1
    assert merged[0].retrieval_score == 0.8
    assert merged[0].retrieval_score_type == "bm25"
    assert merged[0].rank == 0
    assert len(merged[0].evidence) == 2
    assert {entry.graph.seed_memory_id for entry in merged[0].evidence if entry.graph} == {"seed-b"}
