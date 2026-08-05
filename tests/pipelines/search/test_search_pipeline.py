from __future__ import annotations

from types import SimpleNamespace

import pytest
from mindmemos.components.searcher.final_filter import SearchFinalFilter
from mindmemos.components.searcher.scored_candidate import (
    GraphPathEvidence,
    RetrievalEvidence,
    ScoredSearchCandidate,
)
from mindmemos.pipelines.search.base import SearchEngineOptions
from mindmemos.pipelines.search.pipeline import SearchPipelineImpl
from mindmemos.typing.memory import MemoryRequestContext
from mindmemos.typing.service import MemorySearchItem, SearchPipelineInput


def make_context() -> MemoryRequestContext:
    return MemoryRequestContext(
        request_id="req-1",
        account_id="acc-1",
        project_id="proj-1",
        api_key_uuid="key-1",
        user_id="user-1",
        session_id="session-1",
    )


class FakeEngine:
    name = "default"

    def __init__(self) -> None:
        self.inputs: list[SearchPipelineInput] = []

    async def search_candidates(
        self,
        inp: SearchPipelineInput,
        context: MemoryRequestContext,
        *,
        options: SearchEngineOptions | None = None,
    ) -> list[MemorySearchItem]:
        self.inputs.append(inp)
        return [MemorySearchItem(id="mem-1", memory=f"{inp.search_pipeline}:{inp.query}", last_update_at="")]


class ExplodingAgenticWrapper:
    async def run(self, inp, context, engine):
        raise AssertionError("agentic wrapper should not run for non-agentic search")


class BudgetEngine(FakeEngine):
    async def search_candidates(self, inp, context, *, options=None):
        items = [
            ("long", "one two three four five", 1.0),
            ("medium", "one two", 0.8),
            ("short", "one", 0.6),
        ]
        return [
            ScoredSearchCandidate(
                item=MemorySearchItem(id=memory_id, memory=text, last_update_at=""),
                original_rank=rank,
                rank=rank,
                relevance_score=relevance,
                final_score_source="retrieval",
            )
            for rank, (memory_id, text, relevance) in enumerate(items)
        ]


class ProvenanceEngine(FakeEngine):
    async def search_candidates(self, inp, context, *, options=None):
        return [
            ScoredSearchCandidate(
                item=MemorySearchItem(id="safe", memory="safe memory", last_update_at=""),
                original_rank=0,
                rank=0,
                retrieval_score=0.8,
                retrieval_score_type="rrf",
                relevance_score=1.0,
                final_score_source="retrieval",
                evidence=[
                    RetrievalEvidence(
                        source="graph",
                        query="provider-secret-query",
                        engine="internal-engine",
                        graph=GraphPathEvidence(
                            seed_memory_id=f"seed-{index}",
                            relation="relates_to",
                            path_score=0.8 - index / 100,
                        ),
                    )
                    for index in range(10)
                ],
            )
        ]


@pytest.mark.asyncio
async def test_search_pipeline_uses_selected_engine_without_agentic_wrapper() -> None:
    engine = FakeEngine()
    pipeline = SearchPipelineImpl(
        engines={"default": engine},
        agentic_wrapper=ExplodingAgenticWrapper(),
        final_filter=SearchFinalFilter(),
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    result = await pipeline.search(SearchPipelineInput(query="Qdrant", search_pipeline="default"), make_context())

    assert result.memories[0].id == "mem-1"
    assert engine.inputs[0].agentic is False


@pytest.mark.asyncio
async def test_search_pipeline_rejects_unknown_strategy_with_available_names() -> None:
    pipeline = SearchPipelineImpl(
        engines={"default": FakeEngine()},
        final_filter=SearchFinalFilter(),
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="Available strategies: default"):
        await pipeline.search(SearchPipelineInput(query="Qdrant", search_pipeline="schema"), make_context())


@pytest.mark.asyncio
async def test_search_pipeline_exposes_query_local_scores_only_when_requested() -> None:
    pipeline = SearchPipelineImpl(
        engines={"default": FakeEngine()},
        final_filter=SearchFinalFilter(),
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    hidden = await pipeline.search(
        SearchPipelineInput(query="Qdrant", search_pipeline="default"),
        make_context(),
    )
    visible = await pipeline.search(
        SearchPipelineInput(query="Qdrant", search_pipeline="default", include_scores=True),
        make_context(),
    )

    assert "relevance" not in hidden.memories[0].model_dump()
    assert visible.memories[0].relevance is not None
    assert visible.memories[0].relevance.score == 1.0
    assert visible.memories[0].relevance.scope == "query_local"
    assert visible.memories[0].relevance.source == "rank_fallback"


@pytest.mark.asyncio
async def test_score_projection_is_allow_listed_and_caps_graph_provenance() -> None:
    pipeline = SearchPipelineImpl(
        engines={"default": ProvenanceEngine()},
        final_filter=SearchFinalFilter(),
        score_provenance_limit=3,
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    result = await pipeline.search(
        SearchPipelineInput(query="safe", include_scores=True),
        make_context(),
    )

    serialized = result.memories[0].model_dump_json()
    assert len(result.memories[0].relevance.graph) == 3
    assert "provider-secret-query" not in serialized
    assert "internal-engine" not in serialized

@pytest.mark.asyncio
async def test_ordinal_only_rerank_success_is_recorded_without_score_output() -> None:
    async def rerank(client, query, documents, top_n):
        return [1, 0]

    pipeline = SearchPipelineImpl(
        engines={"default": BudgetEngine()},
        final_filter=SearchFinalFilter(
            rerank_client=SimpleNamespace(available=True, has_external_model=True),
            rerank_fn=rerank,
        ),
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    result = await pipeline.search(
        SearchPipelineInput(query="one", rerank=True, top_k=2),
        make_context(),
    )

    assert [item.id for item in result.memories] == ["medium", "long"]
    assert result.metrics["rerank_outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_successful_rerank_is_recorded_when_threshold_removes_every_candidate() -> None:
    async def rerank_with_scores(client, query, documents, top_n):
        return [(0, 0.4), (1, 0.3), (2, 0.2)]

    pipeline = SearchPipelineImpl(
        engines={"default": BudgetEngine()},
        final_filter=SearchFinalFilter(
            rerank_client=SimpleNamespace(available=True, has_external_model=True),
            rerank_with_scores_fn=rerank_with_scores,
        ),
        db_reader=SimpleNamespace(),
        db_writer=SimpleNamespace(),
    )

    result = await pipeline.search(
        SearchPipelineInput(query="one", rerank=True, score_threshold=0.9),
        make_context(),
    )

    assert result.memories == []
    assert result.metrics["rerank_outcome"] == "succeeded"
