"""Configuration owned by the reusable message-chunking component."""

from dataclasses import dataclass, field


@dataclass
class MessageChunkerConfig:
    """Configure turn grouping, chunk planning, history, and overflow compaction.

    All values are token-based; message and turn counts are used only for
    observability.

    Budget hierarchy::

        chunk_hard_token_budget (total prompt limit, e.g. 32000)
        ├── template_tokens      (prompt template overhead)
        ├── output_headroom      (reserved for LLM output)
        ├── recall_budget        (related memory recall context)
        ├── history_hard_token_budget (history context cap)
        └── extractable_budget   (remaining for chunk messages)
            = hard - template - output - recall - history_hard

        chunk_soft_token_budget (target, e.g. 26000)
        └── soft_extractable_budget
            = soft - template - output - recall - history_soft

    ``VanillaAddConfig`` composes this shared configuration without owning it,
    so other extraction algorithms can reuse ``MessageChunker`` directly.
    """

    chunk_soft_token_budget: int = field(default=26000)
    """Soft target for total chunk prompt tokens. Turns are not split to hit this."""

    chunk_hard_token_budget: int = field(default=32000)
    """Maximum total prompt tokens per chunk (template + extractable + history + recall + output)."""

    turn_hard_token_budget: int = field(default=16000)
    """Token threshold above which a single turn is flagged for long-turn compaction."""

    history_soft_token_budget: int = field(default=2000)
    """Target token count for packed history context."""

    history_hard_token_budget: int = field(default=4000)
    """Maximum token count for packed history. Hard cap prevents prompt growth."""

    history_min_turn_count: int = field(default=1)
    """Minimum complete turns to include in history, even if they exceed soft budget."""

    compaction_soft_token_budget: int = field(default=16000)
    """Soft target for compacted long turns; preserving the first user message may exceed it."""

    compaction_head_tokens: int = field(default=4000)
    """Tokens preserved from the start of a long turn as raw evidence."""

    compaction_tail_tokens: int = field(default=4000)
    """Tokens preserved from the end of a long turn as raw evidence."""

    compaction_summary_context_token_budget: int = field(default=200000)
    """Maximum middle-section tokens sent in one long-turn summary call."""

    compaction_summary_output_token_budget: int = field(default=8000)
    """Maximum generated tokens for each long-turn summary call."""

    time_gap_threshold_seconds: int = field(default=1800)
    """Timestamp gap (seconds) that splits consecutive same-role messages into separate turns."""

    template_tokens: int = field(default=1000)
    """Estimated tokens consumed by the extraction prompt template and system instructions."""

    recall_budget: int = field(default=2000)
    """Token budget allocated for related memory recall context in the prompt."""

    output_headroom: int = field(default=4000)
    """Token headroom reserved for the LLM output (extraction results)."""
