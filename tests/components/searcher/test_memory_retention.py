from datetime import UTC, datetime
from types import SimpleNamespace

from mindmemos.components.searcher.memory_retention import MemoryRetentionSelector
from mindmemos.components.searcher.scored_candidate import ScoredSearchCandidate
from mindmemos.config.algo.search.retention import MemoryRetentionConfig
from mindmemos.typing.service import MemorySearchItem


class FakeTextPreprocessor:
    def preprocess_query(self, text: str, *, include_entities: bool = False):
        return SimpleNamespace(tokens=text.lower().split())

    def preprocess_text(self, text: str, *, include_entities: bool = False):
        return SimpleNamespace(tokens=text.lower().split())


def candidate(
    memory_id: str,
    text: str,
    *,
    rank: int,
    relevance: float,
    event_time: str | None = None,
    source_timestamp: str | None = None,
    last_update_at: str = "",
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        item=MemorySearchItem(
            id=memory_id,
            memory=text,
            last_update_at=last_update_at,
            event_time=event_time,
            source_timestamp=source_timestamp,
        ),
        original_rank=rank,
        rank=rank,
        relevance_score=relevance,
        final_score_source="retrieval",
    )


def selector(**updates) -> MemoryRetentionSelector:
    values = {
        "relevance_weight": 1.0,
        "query_overlap_weight": 0.0,
        "recency_weight": 0.0,
        "cost_weight": 0.0,
        **updates,
    }
    return MemoryRetentionSelector(
        config=MemoryRetentionConfig(**values),
        text_preprocessor=FakeTextPreprocessor(),
        now=lambda: datetime(2026, 1, 31, tzinfo=UTC),
    )


def test_retention_skips_oversized_candidate_and_continues_with_later_items() -> None:
    candidates = [
        candidate("long", "one two three four five", rank=0, relevance=1.0),
        candidate("medium", "one two", rank=1, relevance=0.8),
        candidate("short", "one", rank=2, relevance=0.6),
    ]

    result = selector().select(query="one", candidates=candidates, token_budget=3)

    assert [entry.id for entry in result.candidates] == ["medium", "short"]
    assert result.estimated_tokens_before == 8
    assert result.estimated_tokens_after == 3
    assert result.budget_induced_empty is False


def test_retention_returns_empty_when_no_candidate_fits_strict_budget() -> None:
    result = selector().select(
        query="one",
        candidates=[candidate("long", "one two", rank=0, relevance=1.0)],
        token_budget=1,
    )

    assert result.candidates == []
    assert result.estimated_tokens_after == 0
    assert result.budget_induced_empty is True


def test_retention_uses_event_time_before_newer_database_update_time() -> None:
    scorer = selector(relevance_weight=0.0, recency_weight=1.0)
    old_event_recent_update = candidate(
        "old-event",
        "memory",
        rank=0,
        relevance=0.0,
        event_time="2025-01-01 00:00:00",
        last_update_at="2026-01-30 00:00:00",
    )
    recent_event = candidate(
        "recent-event",
        "memory",
        rank=1,
        relevance=0.0,
        event_time="2026-01-30 00:00:00",
        last_update_at="2025-01-01 00:00:00",
    )

    scores = scorer.score(query="memory", candidates=[old_event_recent_update, recent_event], token_budget=10)

    assert scores[1].recency > scores[0].recency
    assert scores[1].priority > scores[0].priority


def test_retention_applies_cost_penalty_once_without_density_division() -> None:
    scorer = selector(relevance_weight=1.0, cost_weight=0.1)
    short = candidate("short", "one", rank=0, relevance=0.8)
    long = candidate("long", "one two three four", rank=1, relevance=1.0)

    scores = scorer.score(query="one", candidates=[short, long], token_budget=10)

    assert scores[0].priority == 0.79
    assert scores[1].priority == 0.96
    assert scores[1].priority > scores[0].priority


def test_retention_handles_invalid_missing_and_future_business_times() -> None:
    scorer = selector(relevance_weight=0.0, recency_weight=1.0, missing_recency_score=0.4)
    scores = scorer.score(
        query="memory",
        candidates=[
            candidate("invalid", "memory", rank=0, relevance=0.0, event_time="not-a-time"),
            candidate("missing", "memory", rank=1, relevance=0.0),
            candidate("future", "memory", rank=2, relevance=0.0, event_time="2027-01-01 00:00:00"),
        ],
        token_budget=10,
    )

    assert scores[0].recency == 0.4
    assert scores[1].recency == 0.4
    assert scores[2].recency == 1.0


def test_retention_deduplicates_identity_and_accepts_exact_budget() -> None:
    duplicate_low = candidate("same", "one two", rank=1, relevance=0.2)
    duplicate_high = candidate("same", "one two", rank=0, relevance=0.9)

    result = selector().select(
        query="one",
        candidates=[duplicate_low, duplicate_high],
        token_budget=2,
    )

    assert [entry.id for entry in result.candidates] == ["same"]
    assert result.estimated_tokens_before == 2
    assert result.estimated_tokens_after == 2


def test_retention_uses_rank_then_id_for_equal_priority_ties() -> None:
    result = selector(relevance_weight=0.0).select(
        query="",
        candidates=[
            candidate("b", "one", rank=0, relevance=0.0),
            candidate("a", "two", rank=0, relevance=0.0),
        ],
        token_budget=1,
    )

    assert [entry.id for entry in result.candidates] == ["a"]
