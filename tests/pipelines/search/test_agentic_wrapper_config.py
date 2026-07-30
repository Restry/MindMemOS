from __future__ import annotations

from mindmemos.components.memory_modeling.schema import TemporalEntity
from mindmemos.components.searcher.scored_candidate import RetrievalEvidence
from mindmemos.config.algo.search import AgenticConfig
from mindmemos.pipelines.search.agentic.loop import _merge_agentic_evidence
from mindmemos.pipelines.search.agentic.wrapper import (
    _agentic_config_with_max_rounds,
    _entities_to_scored_candidates,
)
from omegaconf import OmegaConf


def test_agentic_config_with_max_rounds_supports_dataclass_config() -> None:
    config = AgenticConfig(max_rounds=5, top_k_per_round=12)

    updated = _agentic_config_with_max_rounds(config, 2)

    assert updated.max_rounds == 2
    assert updated.top_k_per_round == 12
    assert config.max_rounds == 5


def test_agentic_config_with_max_rounds_supports_omegaconf_dict_config() -> None:
    config = OmegaConf.create({"max_rounds": 5, "top_k_per_round": 12})

    updated = _agentic_config_with_max_rounds(config, 2)

    assert updated.max_rounds == 2
    assert updated.top_k_per_round == 12
    assert config.max_rounds == 5


def test_agentic_config_with_max_rounds_supports_plain_dict_config() -> None:
    config = {"max_rounds": 5, "top_k_per_round": 12}

    updated = _agentic_config_with_max_rounds(config, 2)

    assert updated.max_rounds == 2
    assert updated.top_k_per_round == 12
    assert config["max_rounds"] == 5


def test_agentic_entities_use_explicit_rank_fallback_scores() -> None:
    entities = [
        TemporalEntity(entity_id="a", name="A", entity_type="person", description="first"),
        TemporalEntity(entity_id="b", name="B", entity_type="person", description="second"),
    ]

    candidates = _entities_to_scored_candidates(entities, include_edges=False, output_max_edge_num=2)

    assert [candidate.id for candidate in candidates] == ["a", "b"]
    assert [candidate.retrieval_score for candidate in candidates] == [None, None]
    assert [candidate.final_score_source for candidate in candidates] == ["rank_fallback", "rank_fallback"]
    assert [candidate.relevance_score for candidate in candidates] == [1.0, 0.0]
    assert [candidate.evidence[0].source for candidate in candidates] == ["agentic", "agentic"]


def test_agentic_repeated_identity_merges_bounded_round_evidence() -> None:
    existing = TemporalEntity(entity_id="same", name="same", entity_type="memory", description="first")
    incoming = TemporalEntity(entity_id="same", name="same", entity_type="memory", description="second")
    existing._search_evidence = [RetrievalEvidence(source="agentic", query="first", round_index=1, engine="default")]
    incoming._search_evidence = [RetrievalEvidence(source="agentic", query="second", round_index=2, engine="default")]

    _merge_agentic_evidence(existing, incoming)
    candidates = _entities_to_scored_candidates(
        [existing],
        include_edges=False,
        output_max_edge_num=2,
        evidence_limit=1,
    )

    assert len(candidates[0].evidence) == 1
    assert candidates[0].evidence[0].query == "first"
    assert candidates[0].final_score_source == "rank_fallback"
