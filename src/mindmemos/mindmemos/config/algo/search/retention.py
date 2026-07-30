"""Token-budget memory retention configuration."""

from dataclasses import dataclass, field


@dataclass
class MemoryRetentionConfig:
    """Validated request bounds and deterministic mixed-score parameters."""

    min_token_budget: int = field(default=1)
    max_token_budget: int = field(default=128000)
    max_candidates: int = field(default=100)
    relevance_weight: float = field(default=0.50)
    query_overlap_weight: float = field(default=0.25)
    recency_weight: float = field(default=0.15)
    cost_weight: float = field(default=0.10)
    recency_half_life_days: float = field(default=30.0)
    missing_recency_score: float = field(default=0.5)
    graph_provenance_limit: int = field(default=8)
    selector_version: str = field(default="mixed-v1")
    estimator_version: str = field(default="heuristic-v1")
