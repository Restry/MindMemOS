"""Claude Code CLI agent implementation.

Wraps ``claude -p`` as a MindMemOS agent.  Skills are written to a temporary
workspace's ``.claude/skills/`` directory so Claude Code discovers and loads
them natively, rather than injecting them as plain text in the system prompt.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..registry import register
from ..typing import AgentType
from ._claude_messages import convert_assistant_blocks, convert_user_blocks
from ._skill_utils import build_skill_bindings, prepare_skills_workspace
from ._trajectory import build_trajectory
from .base import Agent
from .config import ClaudeAgentConfig
from .typing import AgentExecutionRequest, AgentExecutionResult


def _parse_json_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _extract_session_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        sid = event.get("session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return None


def _extract_trajectory_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stream events to OpenAI-format messages.

    The stream contains assistant and user-turn messages.  Each is converted
    to the trajectory's OpenAI message format via the shared Claude message helpers.
    """
    messages: list[dict[str, Any]] = []
    for event in events:
        t = event.get("type")
        if t == "assistant":
            blocks = event.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                messages.append(convert_assistant_blocks(blocks))
        elif t == "user":
            blocks = event.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                messages.extend(convert_user_blocks(blocks))
    return messages


def _extract_num_turns(events: list[dict[str, Any]]) -> int:
    for event in reversed(events):
        if event.get("type") == "result":
            turns = event.get("num_turns")
            if isinstance(turns, int) and turns > 0:
                return turns
    return 1


@register(type="agent", name=AgentType.CLAUDE.value)
class ClaudeAgent(Agent[ClaudeAgentConfig]):
    config_type = ClaudeAgentConfig

    def __init__(self, config: ClaudeAgentConfig | Mapping[str, Any]) -> None:
        super().__init__(config)
        self._cli_path: str | None = None

    async def execute(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        execution_id = request.metadata.get("execution_id") or uuid4().hex
        started_at = time.time()

        trajectory_messages: list[dict[str, Any]] = []
        if request.task.system_prompt:
            trajectory_messages.append({"role": "system", "content": request.task.system_prompt})
        trajectory_messages.append({"role": "user", "content": request.task.instruction})

        # Resolve CLI path early so we don't create temp dirs on failure.
        try:
            cli = self._resolve_cli()
        except RuntimeError as exc:
            return self._error_result(request, execution_id, started_at, trajectory_messages, str(exc))

        # Write skills to a temporary .claude/skills/ workspace.
        skill_workspace = prepare_skills_workspace(request.skills)

        # Build the claude -p command.
        cmd = [cli, "-p", request.task.instruction]
        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.config.max_turns:
            cmd += ["--max-turns", str(self.config.max_turns)]
        if request.task.system_prompt:
            cmd += ["--system-prompt", request.task.system_prompt]
        if skill_workspace:
            # --add-dir exposes the temp .claude/skills/ directory so Claude
            # can discover and load skills independently of the task cwd.
            cmd += ["--add-dir", skill_workspace]
        cmd += ["--output-format", "stream-json", "--verbose"]
        if self.config.dangerously_skip_permissions:
            cmd += ["--dangerously-skip-permissions"]

        timeout = self.config.timeout_seconds
        # cwd sets Claude's working directory (file access to workspace).
        cwd = request.workspace or None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # cwd gives Claude access to workspace files via the process
                # working directory.  --add-dir (above) gives Claude access to
                # the isolated .claude/skills/ temp dir.  Both are needed;
                # Claude scans both for skill discovery.
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return self._error_result(
                request,
                execution_id,
                started_at,
                trajectory_messages,
                f"Claude CLI timed out after {timeout}s",
            )
        except Exception as e:
            return self._error_result(
                request,
                execution_id,
                started_at,
                trajectory_messages,
                f"Claude CLI execution failed: {e}",
            )

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = (stderr.decode("utf-8", errors="replace") or "").strip() if stderr else ""

        events = _parse_json_events(stdout_text)
        session_id = _extract_session_id(events)
        num_turns = _extract_num_turns(events)
        stream_messages = _extract_trajectory_messages(events)
        ended_at = time.time()
        is_success = proc.returncode == 0

        # Append all stream messages (assistant + user) for a faithful trace.
        trajectory_messages.extend(stream_messages)

        # Only skills actually invoked via the Skill tool count as used.
        skill_bindings = build_skill_bindings(request.skills, trajectory_messages)

        trajectory = build_trajectory(
            request=request,
            config=self.config,
            execution_id=execution_id,
            messages=trajectory_messages,
            skill_bindings=skill_bindings,
            started_at=started_at,
            ended_at=ended_at,
            n_turn=num_turns,
            is_success=is_success,
            error_info=stderr_text if not is_success else None,
            agent_type=AgentType.CLAUDE,
        )
        # Expose session_id for potential resumption.
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

    def _resolve_cli(self) -> str:
        if self._cli_path is not None:
            return self._cli_path
        executable = self.config.cli_path or "claude"
        path = shutil.which(executable)
        if not path:
            raise RuntimeError(f"Claude CLI executable {executable!r} was not found.")
        self._cli_path = path
        return path

    def _error_result(
        self,
        request: AgentExecutionRequest,
        execution_id: str,
        started_at: float,
        messages: list[dict[str, Any]],
        error_info: str,
    ) -> AgentExecutionResult:
        ended_at = time.time()
        skill_bindings = build_skill_bindings(request.skills, messages)
        return AgentExecutionResult(
            execution_id=execution_id,
            task=request.task,
            skills=request.skills,
            skill_bindings=skill_bindings,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=ended_at - started_at,
            trajectory=build_trajectory(
                request=request,
                config=self.config,
                execution_id=execution_id,
                messages=messages,
                skill_bindings=skill_bindings,
                started_at=started_at,
                ended_at=ended_at,
                n_turn=0,
                is_success=False,
                error_info=error_info,
                agent_type=AgentType.CLAUDE,
            ),
        )
