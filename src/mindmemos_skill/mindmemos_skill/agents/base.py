"""OpenAI ReAct Agent, openclaw, claude, codex"""
from typing import Protocol

from .typing import AgentExecutionRequest, AgentExecutionResult


class Agent(Protocol):
    async def execute(
            self,
            request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        ...
