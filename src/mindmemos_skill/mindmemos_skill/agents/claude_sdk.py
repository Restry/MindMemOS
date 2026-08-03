"""Claude Agent SDK agent implementation.

Uses ``claude_agent_sdk`` (``query`` + streaming events) instead of the
``claude -p`` subprocess.  Skills are written to a temporary workspace's
``.claude/skills/`` directory and discovered natively by the SDK.
"""

from __future__ import annotations

import dataclasses
import time
from uuid import uuid4
from typing import Any

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    UserMessage,
)

from ._compat import convert_assistant_blocks, convert_user_blocks
from .base import Agent
from .typing import AgentExecutionRequest, AgentExecutionResult
from ..registry import register
from ..typing import Skill, SkillBinding, SkillUsageType, Trajectory
from .claude import _prepare_skills_workspace


# SDK block type → plain-dict type string
_BLOCK_TYPE_MAP = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
}


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert one SDK content block to a plain dict the ``_compat`` helpers expect."""
    if dataclasses.is_dataclass(block):
        d = dataclasses.asdict(block)
        d.setdefault("type", _BLOCK_TYPE_MAP.get(type(block).__name__, type(block).__name__))
        return d
    return {} if not isinstance(block, dict) else block


def _message_to_dict(msg: Any) -> dict[str, Any] | list[dict[str, Any]]:
    """Convert an SDK Message to one or more OpenAI-format messages."""
    blocks = [_block_to_dict(b) for b in (msg.content or [])]
    if isinstance(msg, AssistantMessage):
        return convert_assistant_blocks(blocks)
    if isinstance(msg, UserMessage):
        return convert_user_blocks(blocks)  # returns list
    return {}


@register(type="agent", name="claude-sdk")
class ClaudeSDKAgent:
    """Agent that uses ``claude_agent_sdk`` to execute tasks with skill support."""

    def __init__(self) -> None:
        pass

    async def execute(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        execution_id = request.metadata.get("execution_id") or uuid4().hex
        started_at = time.time()

        skill_workspace = _prepare_skills_workspace(request.skills)

        # Record input trajectory.
        trajectory_messages: list[dict[str, Any]] = []
        if request.task.system_prompt:
            trajectory_messages.append(
                {"role": "system", "content": request.task.system_prompt}
            )
        trajectory_messages.append(
            {"role": "user", "content": request.task.instruction}
        )

        # Build SDK options.
        add_dirs: list[str] = []
        if skill_workspace:
            add_dirs.append(skill_workspace)

        options = ClaudeAgentOptions(
            system_prompt=request.task.system_prompt,
            add_dirs=add_dirs if add_dirs else None,
            permission_mode="bypassPermissions",
            cwd=request.workspace or None,
            max_turns=request.max_turns,
        )

        # Track result metadata from the stream.
        session_id: str | None = None
        num_turns: int = 0
        result_text: str = ""
        is_success = False

        try:
            async for message in query(
                prompt=request.task.instruction,
                options=options,
            ):
                if isinstance(message, ResultMessage):
                    session_id = getattr(message, "session_id", None) or session_id
                    num_turns = getattr(message, "num_turns", 0) or 0
                    result_text = message.result or ""
                    is_success = not message.is_error
                elif isinstance(message, AssistantMessage):
                    trajectory_messages.append(_message_to_dict(message))
                elif isinstance(message, UserMessage):
                    trajectory_messages.extend(_message_to_dict(message))  # returns list
        except Exception:
            is_success = False

        ended_at = time.time()

        skill_bindings = [
            SkillBinding(
                name=s.name,
                content_hash=str(hash(s.content or "")),
                usage=SkillUsageType.INJECTED,
            )
            for s in request.skills
        ]

        # Ensure at least one assistant message if we have a result.
        if result_text and not any(
            m.get("role") == "assistant" for m in trajectory_messages
        ):
            trajectory_messages.append(
                {"role": "assistant", "content": result_text}
            )

        trajectory = Trajectory(
            messages=trajectory_messages,
            input_skills=request.skills,
            used_skills=request.skills,
            started_at=started_at,
            finished_at=ended_at,
            duration_s=ended_at - started_at,
            n_turn=num_turns or 1,
            is_success=is_success,
            error_info=None if is_success else "SDK query failed",
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
