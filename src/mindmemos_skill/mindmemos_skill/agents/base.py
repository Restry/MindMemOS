"""Base contract shared by all agent implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel

from .config import AgentConfig
from .typing import AgentExecutionRequest, AgentExecutionResult

AgentConfigT = TypeVar("AgentConfigT", bound=AgentConfig)


class Agent(ABC, Generic[AgentConfigT]):
    """Configured executable agent.

    Concrete agents declare ``config_type`` so mapping inputs are validated at
    construction time rather than being interpreted ad hoc during execution.
    """

    config_type: type[AgentConfig] = AgentConfig

    def __init__(self, config: AgentConfigT | Mapping[str, Any]) -> None:
        raw_config = config.model_dump() if isinstance(config, BaseModel) else config
        self.config = cast(AgentConfigT, self.config_type.model_validate(raw_config))

    @abstractmethod
    async def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        """Execute one task using this agent's validated configuration."""


__all__ = ["Agent", "AgentConfigT"]
