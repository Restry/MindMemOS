"""Claude Code CLI agent implementation.

Wraps ``claude -p`` as a MindMemOS agent.  Skills are written to a temporary
workspace's ``.claude/skills/`` directory so Claude Code discovers and loads
them natively, rather than injecting them as plain text in the system prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from uuid import uuid4
from typing import Any

from ._compat import convert_assistant_blocks, convert_user_blocks
from .base import Agent
from .typing import AgentExecutionRequest, AgentExecutionResult
from ..registry import register
from ..typing import Skill, SkillBinding, SkillUsageType, Trajectory

_AGENT_TIMEOUT_SEC = 300


def _skill_to_markdown(skill: Skill) -> str:
    """Render a ``Skill`` object as a SKILL.md file with YAML frontmatter.

    ``linked_files`` are NOT embedded here — they are written as actual files
    alongside the SKILL.md so Claude can access them directly.
    """

    parts = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
        "---",
        "",
    ]
    if skill.content:
        parts.append(skill.content)
        parts.append("")

    return "\n".join(parts)


def _prepare_skills_workspace(skills: list[Skill]) -> str | None:
    """Write skills to ``<tempdir>/.claude/skills/`` and return the tempdir path.

    Each skill becomes one ``<name>.md`` file under ``.claude/skills/``.
    Skill ``linked_files`` (scripts, configs, etc.) are written as actual
    files in the temp workspace so Claude can access them via Bash/Read.

    The returned directory can be passed to ``claude --add-dir`` so Claude
    loads the skills natively.  Directories are timestamped and left on disk
    for inspection; the system temp directory is expected to be cleaned
    periodically by the OS.

    Returns ``None`` when there are no skills to prepare.
    """
    if not skills:
        return None

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    workspace = tempfile.mkdtemp(prefix=f"mindmemos_skills_{timestamp}_")
    skills_dir = os.path.join(workspace, ".claude", "skills")
    os.makedirs(skills_dir, exist_ok=True)

    for skill in skills:
        # Sanitize the name into a safe filename
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in skill.name)
        skill_dir = os.path.join(skills_dir, safe_name)
        os.makedirs(skill_dir, exist_ok=True)
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(_skill_to_markdown(skill))

        # Write linked_files inside the skill directory so Claude can find
        # them relative to the SKILL.md location.
        abs_workspace = os.path.abspath(workspace)
        file_refs: list[str] = []
        for rel_path, content in skill.linked_files.items():
            abs_path = os.path.abspath(os.path.join(skill_dir, rel_path))
            # Guard against path traversal outside the temp workspace
            if not abs_path.startswith(abs_workspace + os.sep):
                continue
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            file_refs.append(f"- `{rel_path}` — 和 SKILL.md 在同一目录")
        if file_refs:
            with open(skill_path, "a", encoding="utf-8") as f:
                f.write("\n\n## 关联文件\n\n" + "\n".join(file_refs))

    return workspace


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
    to OpenAI Chat Completions format via the shared ``_compat`` helpers.
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


@register(type="agent", name="claude")
class ClaudeAgent(Agent):

    def __init__(self) -> None:
        self._cli_path: str | None = None

    async def execute(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResult:
        execution_id = request.metadata.get("execution_id") or uuid4().hex
        started_at = time.time()

        # Resolve CLI path early so we don't create temp dirs on failure.
        cli = self._resolve_cli()

        # Write skills to a temporary .claude/skills/ workspace.
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

        # Build the claude -p command.
        cmd = [cli, "-p", request.task.instruction]
        if request.task.system_prompt:
            cmd += ["--system-prompt", request.task.system_prompt]
        if skill_workspace:
            # --add-dir exposes the temp .claude/skills/ directory so Claude
            # can discover and load skills from it.  This is separate from
            # cwd (below) which gives Claude access to the user's workspace.
            cmd += ["--add-dir", skill_workspace]
        cmd += ["--output-format", "stream-json", "--verbose"]
        cmd += ["--dangerously-skip-permissions"]

        timeout = request.metadata.get("timeout", _AGENT_TIMEOUT_SEC)
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return self._error_result(
                request, execution_id, started_at,
                trajectory_messages,
                f"Claude CLI timed out after {timeout}s",
            )
        except Exception as e:
            return self._error_result(
                request, execution_id, started_at,
                trajectory_messages,
                f"Claude CLI execution failed: {e}",
            )

        stdout_text = (
            stdout.decode("utf-8", errors="replace") if stdout else ""
        )
        stderr_text = (
            (stderr.decode("utf-8", errors="replace") or "").strip()
            if stderr
            else ""
        )

        events = _parse_json_events(stdout_text)
        session_id = _extract_session_id(events)
        num_turns = _extract_num_turns(events)
        stream_messages = _extract_trajectory_messages(events)
        ended_at = time.time()
        is_success = proc.returncode == 0

        # Append all stream messages (assistant + user) for a faithful trace.
        trajectory_messages.extend(stream_messages)

        skill_bindings = [
            SkillBinding(
                name=s.name,
                content_hash=str(hash(s.content or "")),
                usage=SkillUsageType.INJECTED,
            )
            for s in request.skills
        ]

        trajectory = Trajectory(
            messages=trajectory_messages,
            input_skills=request.skills,
            used_skills=request.skills,
            started_at=started_at,
            finished_at=ended_at,
            duration_s=ended_at - started_at,
            n_turn=num_turns,
            is_success=is_success,
            error_info=stderr_text if not is_success else None,
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
        path = shutil.which("claude")
        if not path:
            raise RuntimeError(
                "`claude` CLI not found in PATH. Install Claude Code first."
            )
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
        return AgentExecutionResult(
            execution_id=execution_id,
            task=request.task,
            skills=request.skills,
            skill_bindings=[],
            started_at=started_at,
            ended_at=ended_at,
            duration_s=ended_at - started_at,
            trajectory=Trajectory(
                messages=messages,
                input_skills=request.skills,
                used_skills=[],
                started_at=started_at,
                finished_at=ended_at,
                duration_s=ended_at - started_at,
                n_turn=0,
                is_success=False,
                error_info=error_info,
            ),
        )
