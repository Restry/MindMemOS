"""Validated construction-time configuration for built-in agents."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentConfig(BaseModel):
    """Configuration shared by every agent implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = Field(default=None, min_length=1)
    max_turns: int | None = Field(default=None, ge=1)

    def snapshot(self) -> dict[str, Any]:
        """Return a secret-free, JSON-compatible trajectory snapshot."""
        return self.model_dump(mode="json", exclude_none=True)


class ClaudeAgentConfig(AgentConfig):
    """Configuration specific to the Claude Code CLI agent."""

    cli_path: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=300.0, gt=0)
    dangerously_skip_permissions: bool = False


class ClaudeSDKAgentConfig(AgentConfig):
    """Configuration specific to the Claude Agent SDK agent."""

    permission_mode: str = Field(default="bypassPermissions", min_length=1)


__all__ = [
    "AgentConfig",
    "ClaudeAgentConfig",
    "ClaudeSDKAgentConfig",
]
