"""Vanilla add pipeline configuration."""

from dataclasses import dataclass, field

from ..components import MessageChunkerConfig


@dataclass
class VanillaAddRecallConfig:
    """Related-memory recall configuration for the recall-before-extract component."""

    top_k: int = field(default=5)
    """Maximum related memories returned to the extractor after RRF fusion."""

    scan_limit: int = field(default=100)
    """Maximum active memories scanned per project for hash/entity candidate generation."""

    fusion_k: int = field(default=60)
    """RRF smoothing constant; higher values flatten rank differences between channels."""

    fusion_weight_semantic: float = field(default=1.5)
    """RRF weight for the dense semantic channel."""

    fusion_weight_bm25: float = field(default=1.0)
    """RRF weight for the sparse BM25 channel."""

    fusion_weight_entity: float = field(default=1.2)
    """RRF weight for the entity-overlap channel."""

    fusion_weight_recent: float = field(default=0.5)
    """RRF weight for the recency channel."""

    fusion_weight_schema_property: float = field(default=2.0)
    """RRF weight for the schema-property channel."""


@dataclass
class VanillaAddSafetyGateConfig:
    """Deterministic safety-gate component configuration applied before DB write."""

    min_content_chars: int = field(default=1)
    """Minimum normalized content length; shorter candidates are skipped."""

    min_update_confidence: float = field(default=0.7)
    """Confidence floor for honoring an ``update`` hint; below it the action downgrades to ADD."""

    min_merge_confidence: float = field(default=0.8)
    """Confidence floor for honoring a ``merge`` hint; below it the action downgrades to ADD."""


@dataclass
class VanillaAddConfig:
    """Compose the vanilla add pipeline configuration."""

    enable_entities: bool = field(default=False)
    """Whether vanilla add writes entity nodes, MENTIONS edges, and entity embeddings."""

    chunker: MessageChunkerConfig = field(default_factory=MessageChunkerConfig)
    """Turn grouping, chunk planning, history packing, and long-turn compaction tunables."""

    recall: VanillaAddRecallConfig = field(default_factory=VanillaAddRecallConfig)
    """Related-memory recall tunables; surfaced to the extractor as context."""

    safety_gate: VanillaAddSafetyGateConfig = field(default_factory=VanillaAddSafetyGateConfig)
    """Confidence floors and content checks applied to extractor action hints."""
