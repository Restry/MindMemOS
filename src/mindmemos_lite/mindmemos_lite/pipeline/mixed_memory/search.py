"""Request-time routing to the search pipeline for one memory mode."""

from __future__ import annotations

from collections.abc import Mapping

from ...config import MemoryConfig
from ...logging import traced
from ...typing import MemoryRequestContext, SearchPipelineInput, SearchPipelineResult
from ..base import PipelineBase, SearchPipeline
from ..registry import create_pipeline, register


@register(type="search", name="mode_search")
class ModeSearchPipeline(PipelineBase):
    """Route one search request to exactly one configured memory mode."""

    def __init__(
        self,
        *,
        pipelines: Mapping[str, SearchPipeline],
        default_mode: str,
    ) -> None:
        if not pipelines:
            raise ValueError("mode search requires at least one child pipeline")
        if default_mode not in pipelines:
            raise ValueError(f"default search mode {default_mode!r} is not configured")
        self._pipelines = dict(pipelines)
        self._default_mode = default_mode

    @classmethod
    def from_config(cls, config: MemoryConfig, **kwargs):
        routing = config.pipelines
        pipelines = {
            mode: create_pipeline(
                type="search",
                name=binding.search_pipeline,
                config=config,
                **kwargs,
            )
            for mode, binding in routing.modes.items()
        }
        return cls(pipelines=pipelines, default_mode=routing.default_search_mode)

    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(self._pipelines)

    @traced("search.mode_search")
    async def search(
        self,
        inp: SearchPipelineInput,
        context: MemoryRequestContext,
    ) -> SearchPipelineResult:
        mode = inp.memory_mode or context.memory_algorithm or self._default_mode
        pipeline = self._pipelines.get(mode)
        if pipeline is None:
            available = ", ".join(self._pipelines)
            raise ValueError(f"unknown memory mode {mode!r}; available modes: {available}")

        # The selected mode travels both in the pipeline input (observability)
        # and context (persistence isolation). Child algorithms do not need to
        # know that a routing layer exists.
        child_input = inp.model_copy(update={"memory_mode": mode})
        child_context = context.model_copy(update={"memory_algorithm": mode})
        return await pipeline.search(child_input, child_context)


__all__ = ["ModeSearchPipeline"]
