"""Vanilla add pipeline: 6-phase orchestration with LLM extraction, recall, and safety gate."""

from __future__ import annotations

from typing import Literal

from ...components.extractor.vanilla import (
    AddCoreBuilder,
    AddSafetyGate,
    CandidateDeduplicator,
    RelatedMemoryRecall,
    VanillaMemoryExtractor,
)
from ...components.text import MemoryVectorizer, SparseVectorEncoder, TextPreprocessor, get_text_preprocessor
from ...config import MemoryConfig, TextProcessingConfig, VanillaAddConfig, get_config
from ...errors import ConfigNotInitializedError
from ...llm import get_embed_client, get_llm_client
from ...logging import get_logger, traced
from ...typing import (
    AddPipelineInput,
    AddPipelineSyncResult,
    MemoryDbMutationPlan,
    MemoryDbWritePlan,
    MemoryRequestContext,
)
from ..base import MemoryPersistencePipelineMixin
from ..registry import register

Consistency = Literal["fast", "strong"]
logger = get_logger(__name__)
_CLIENT_UNSET = object()


def _try_get_llm():
    """Try to resolve the global LLM client; return None if unavailable."""
    try:
        return get_llm_client()
    except Exception:
        logger.debug("llm_client_not_available", exc_info=True)
        return None


def _try_get_embed():
    """Try to resolve the global embedding client; return None if unavailable."""
    try:
        return get_embed_client()
    except Exception:
        logger.debug("embed_client_not_available", exc_info=True)
        return None


def _has_writes(plan: MemoryDbWritePlan) -> bool:
    return bool(
        plan.memories or plan.entities or plan.sources or plan.vectors or plan.entity_vectors or plan.relationships
    )


def _default_consistency() -> Consistency:
    value = get_config().database.default_consistency
    return value if value in {"fast", "strong"} else "fast"


def _default_vanilla_add_config() -> VanillaAddConfig:
    try:
        config = get_config().algo_config.add
        return getattr(config, "vanilla", config)
    except ConfigNotInitializedError:
        return VanillaAddConfig()


@register(type="add", name="vanilla_add")
class VanillaAddPipeline(MemoryPersistencePipelineMixin):
    """Vanilla add pipeline with 6-phase orchestration.

    Phases: segment → preprocess → recall → extract → plan → vectorize.
    Supports ADD, REINFORCE, UPDATE, MERGE, and SKIP actions.
    """

    def __init__(
        self,
        *,
        text_config: TextProcessingConfig | None = None,
        text_preprocessor: TextPreprocessor | None = None,
        sparse_encoder: SparseVectorEncoder | None = None,
        memory_extractor=None,
        candidate_deduplicator: CandidateDeduplicator | None = None,
        related_memory_recall: RelatedMemoryRecall | None = None,
        safety_gate: AddSafetyGate | None = None,
        consistency: Consistency | None = None,
        vanilla_add_config: VanillaAddConfig | None = None,
        llm_client=_CLIENT_UNSET,
        embed_client=_CLIENT_UNSET,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        cfg = text_config or get_config().algo_config.text_processing
        self._text_preprocessor = text_preprocessor or get_text_preprocessor(cfg)
        self._sparse_encoder = sparse_encoder or SparseVectorEncoder(cfg)

        # Lazily resolve global LLM/embed singletons when the caller leaves clients unset.
        resolved_llm = _try_get_llm() if llm_client is _CLIENT_UNSET else llm_client
        resolved_embed = _try_get_embed() if embed_client is _CLIENT_UNSET else embed_client

        self._memory_extractor = memory_extractor or VanillaMemoryExtractor(llm_client=resolved_llm)
        self._candidate_deduplicator = candidate_deduplicator or CandidateDeduplicator()
        self._related_memory_recall = related_memory_recall or RelatedMemoryRecall(
            db_reader=self.persistence,
            sparse_encoder=self._sparse_encoder,
        )
        self._safety_gate = safety_gate or AddSafetyGate()
        self._explicit_consistency = consistency
        self._explicit_vanilla_add_config = vanilla_add_config

        vectorizer = MemoryVectorizer(
            sparse_encoder=self._sparse_encoder,
            embed_client=resolved_embed,
            text_preprocessor=self._text_preprocessor,
        )

        self._builder = AddCoreBuilder(
            text_preprocessor=self._text_preprocessor,
            memory_extractor=self._memory_extractor,
            candidate_deduplicator=self._candidate_deduplicator,
            related_memory_recall=self._related_memory_recall,
            safety_gate=self._safety_gate,
            vectorizer=vectorizer,
            llm_client=resolved_llm,
        )

    @classmethod
    def from_config(cls, config: MemoryConfig, **kwargs):
        """Construct the copied algorithm with its original typed config."""

        return cls(
            text_config=config.algo_config.text_processing,
            vanilla_add_config=config.algo_config.add,
            consistency=config.database.default_consistency,
            **kwargs,
        )

    def _get_consistency(self) -> Consistency:
        if self._explicit_consistency is not None:
            return self._explicit_consistency
        return _default_consistency()

    def _get_vanilla_add_config(self) -> VanillaAddConfig:
        if self._explicit_vanilla_add_config is not None:
            return self._explicit_vanilla_add_config
        return _default_vanilla_add_config()

    @traced("add.vanilla_add.sync")
    async def add_sync(
        self,
        inp: AddPipelineInput,
        context: MemoryRequestContext,
    ) -> AddPipelineSyncResult:
        """Synchronously write normalized text memories and their mentioned entities."""

        plan, events, update_commands = await self._builder.build(
            inp,
            context,
            consistency=self._get_consistency(),
            config=self._get_vanilla_add_config(),
        )
        mutation_plan = MemoryDbMutationPlan.from_write_plan(plan)
        mutation_plan.memory_updates.extend(update_commands)
        if mutation_plan.has_writes() or mutation_plan.has_updates_or_deletes():
            write_result = await self.persistence.apply_mutation_plan(
                context,
                mutation_plan,
                consistency=self._get_consistency(),
            )
            if write_result.graph_pending:
                logger.warning(
                    "graph_write_pending",
                    request_id=context.request_id,
                    memory_ids=write_result.memory_ids,
                    errors=write_result.errors,
                )
        return AddPipelineSyncResult(status="ok", memories=events)
