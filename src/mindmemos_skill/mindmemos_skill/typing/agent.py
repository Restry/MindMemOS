"""Agent configuration snapshots used by Skill algorithms."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class AgentType(StrEnum):
    """Agent implementation that produced a trajectory."""

    CLAUDE = "claude"
    CLAUDE_SDK = "claude_sdk"
    CODEX = "codex"
    OPENCLAW = "openclaw"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class AgentProfile(BaseModel):
    """One reproducible, secret-free Agent configuration snapshot."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    agent_type: AgentType = AgentType.UNKNOWN
    """执行轨迹的 Agent 实现。"""

    config: dict[str, JsonValue] = Field(default_factory=dict)
    """模型、提供商、温度、推理参数等非密钥配置。"""


__all__ = ["AgentProfile", "AgentType"]
