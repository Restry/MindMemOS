"""Claude Agent SDK agent implementation.

Uses ``claude_agent_sdk`` (``query`` + streaming events) instead of the
``claude -p`` subprocess.  Skills are written to a temporary workspace's
``.claude/skills/`` directory and discovered natively by the SDK.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..registry import register
from ..typing import AgentType
from ._claude_messages import (
    convert_assistant_blocks,
    convert_user_blocks,
)
from ._skill_utils import build_skill_bindings, prepare_skills_workspace
from ._trajectory import build_trajectory
from .base import Agent
from .config import ClaudeSDKAgentConfig
from .typing import AgentExecutionRequest, AgentExecutionResult

# SDK block type → plain-dict type string
_BLOCK_TYPE_MAP = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
}


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert one SDK content block to a plain dict the Claude message helpers expect."""
    if dataclasses.is_dataclass(block):
        d = dataclasses.asdict(block)
        d.setdefault("type", _BLOCK_TYPE_MAP.get(type(block).__name__, type(block).__name__))
        return d
    return {} if not isinstance(block, dict) else block


def _message_to_dict(
    msg: Any,
    *,
    assistant_message_type: type[Any],
    user_message_type: type[Any],
) -> dict[str, Any] | list[dict[str, Any]]:
    """Convert an SDK Message to one or more OpenAI-format messages."""
    blocks = [_block_to_dict(b) for b in (msg.content or [])]
    if isinstance(msg, assistant_message_type):
        return convert_assistant_blocks(blocks)
    if isinstance(msg, user_message_type):
        return convert_user_blocks(blocks)  # returns list
    return {}


@register(type="agent", name=AgentType.CLAUDE_SDK.value)
class ClaudeSDKAgent(Agent[ClaudeSDKAgentConfig]):
    """Agent that uses ``claude_agent_sdk`` to execute tasks with skill support."""

    config_type = ClaudeSDKAgentConfig

    def __init__(self, config: ClaudeSDKAgentConfig | Mapping[str, Any]) -> None:
        super().__init__(config)

    async def execute(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        from claude_agent_sdk import ClaudeAgentOptions, query
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, UserMessage

        execution_id = request.metadata.get("execution_id") or uuid4().hex
        started_at = time.time()

        skill_workspace = prepare_skills_workspace(request.skills)

        # Record input trajectory.
        trajectory_messages: list[dict[str, Any]] = []
        if request.task.system_prompt:
            trajectory_messages.append({"role": "system", "content": request.task.system_prompt})
        trajectory_messages.append({"role": "user", "content": request.task.instruction})

        # Build SDK options.
        add_dirs: list[str] = []
        if skill_workspace:
            add_dirs.append(skill_workspace)

        # Track result metadata from the stream.
        session_id: str | None = None
        num_turns: int = 0
        result_text: str = ""
        is_success = False
        error_info: str | None = None

        try:
            options = ClaudeAgentOptions(
                system_prompt=request.task.system_prompt,
                add_dirs=add_dirs if add_dirs else None,
                permission_mode=self.config.permission_mode,
                cwd=request.workspace or None,
                max_turns=self.config.max_turns,
                model=self.config.model,
            )
            async for message in query(
                prompt=request.task.instruction,
                options=options,
            ):
                if isinstance(message, ResultMessage):
                    session_id = getattr(message, "session_id", None) or session_id
                    num_turns = getattr(message, "num_turns", 0) or 0
                    result_text = message.result or ""
                    is_success = not message.is_error
                    if message.is_error:
                        error_info = result_text or "Claude Agent SDK returned an error result"
                elif isinstance(message, AssistantMessage):
                    trajectory_messages.append(
                        _message_to_dict(
                            message,
                            assistant_message_type=AssistantMessage,
                            user_message_type=UserMessage,
                        )
                    )
                elif isinstance(message, UserMessage):
                    trajectory_messages.extend(
                        _message_to_dict(
                            message,
                            assistant_message_type=AssistantMessage,
                            user_message_type=UserMessage,
                        )
                    )  # returns list
        except Exception as exc:
            is_success = False
            error_info = f"Claude Agent SDK query failed: {exc}"

        ended_at = time.time()

        # Ensure at least one assistant message if we have a result.
        if result_text and not any(m.get("role") == "assistant" for m in trajectory_messages):
            trajectory_messages.append({"role": "assistant", "content": result_text})

        skill_bindings = build_skill_bindings(request.skills, trajectory_messages)

        trajectory = build_trajectory(
            request=request,
            config=self.config,
            execution_id=execution_id,
            messages=trajectory_messages,
            skill_bindings=skill_bindings,
            started_at=started_at,
            ended_at=ended_at,
            n_turn=num_turns or 1,
            is_success=is_success,
            error_info=error_info if not is_success else None,
            agent_type=AgentType.CLAUDE_SDK,
        )
        validation = {"session_id": session_id} if session_id else None

        return AgentExecutionResult(
            execution_id=execution_id,
            task=request.task,
            skills=request.skills,
            skill_bindings=skill_bindings,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=ended_at - started_at,
            validation=validation,
            trajectory=trajectory,
        )
