"""Default feedback pipeline."""

from __future__ import annotations

from ...persistence import MemoryOperationRecorder
from ...persistence.memory import MemoryPersistence
from ...typing import FeedbackPipelineInput, FeedbackPipelineResult, MemoryRequestContext
from ..base import PipelineBase, SearchPipeline
from ..registry import register
from .executor import FeedbackActionExecutor
from .explicit import ExplicitFeedbackHandler
from .implicit import ImplicitFeedbackHandler, ImplicitFeedbackRecordCollector

MEMORY_FEEDBACK_TOPIC = "memory.feedback"


@register(type="feedback", name="default_feedback")
class DefaultFeedbackPipeline(PipelineBase):
    """Route feedback requests to explicit or implicit handlers."""

    def __init__(
        self,
        *,
        explicit_handler: ExplicitFeedbackHandler | None = None,
        implicit_handler: ImplicitFeedbackHandler | None = None,
    ) -> None:
        self._explicit = explicit_handler
        self._implicit = implicit_handler

    @classmethod
    def from_config(
        cls,
        config,
        *,
        persistence: MemoryPersistence,
        operation_recorder: MemoryOperationRecorder,
        search_pipeline: SearchPipeline,
    ) -> "DefaultFeedbackPipeline":
        executor = FeedbackActionExecutor(
            persistence=persistence,
            text_config=config.algo_config.text_processing,
        )
        return cls(
            explicit_handler=ExplicitFeedbackHandler(
                executor=executor,
                search_pipeline=search_pipeline,
            ),
            implicit_handler=ImplicitFeedbackHandler(
                collector=ImplicitFeedbackRecordCollector(
                    persistence=persistence,
                    operation_recorder=operation_recorder,
                    search_pipeline=search_pipeline,
                ),
                executor=executor,
            ),
        )

    async def feedback(self, inp: FeedbackPipelineInput, context: MemoryRequestContext) -> FeedbackPipelineResult:
        """Compatibility wrapper for callers that still use the old single entrypoint."""

        if inp.mode == "async":
            raise RuntimeError("async feedback must be dispatched through the Lite memory service")
        return await self.feedback_sync(inp, context)

    async def feedback_sync(self, inp: FeedbackPipelineInput, context: MemoryRequestContext) -> FeedbackPipelineResult:
        """Route explicit feedback by payload, otherwise run implicit feedback."""

        if inp.feedback:
            if self._explicit is None:
                self._explicit = ExplicitFeedbackHandler()
            return await self._explicit.run(inp, context)

        if self._implicit is None:
            self._implicit = ImplicitFeedbackHandler()
        return await self._implicit.run(inp, context)

    async def feedback_async(self, inp: FeedbackPipelineInput, context: MemoryRequestContext) -> FeedbackPipelineResult:
        """Keep task submission at the Lite service boundary."""

        del inp, context
        raise RuntimeError("async feedback must be dispatched through the Lite memory service")


__all__ = ["DefaultFeedbackPipeline", "MEMORY_FEEDBACK_TOPIC"]
