"""Agent-family-specific Skill evidence detectors.

The current parser is intentionally named for OpenClaw-style text tool calls.
It does not claim to understand Claude SDK blocks or generic agent messages.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .bundle import frontmatter_value
from .models import DetectedSkillCandidate, DetectedSkillUsage

_TOOL_CALL = re.compile(r"^\s*\[tool_call\]\s*([A-Za-z0-9_.-]+)\((.*)\)\s*$", re.DOTALL)
_SKILL_MD = re.compile(r"(?:^|[/\\])SKILL\.md$")
_USAGE_PRIORITY = {DetectedSkillUsage.INJECTED: 1, DetectedSkillUsage.MODIFIED: 2}


def detect_openclaw_skill_candidates(
    messages: list[BaseModel | dict[str, Any]],
) -> list[DetectedSkillCandidate]:
    """Parse OpenClaw ``[tool_call]`` read/write/edit evidence for SKILL.md."""

    serialized = [message.model_dump() if isinstance(message, BaseModel) else message for message in messages]
    candidates: dict[str, DetectedSkillCandidate] = {}
    for index, message in enumerate(serialized):
        if message.get("role") != "assistant":
            continue
        parsed = _parse_tool_call(str(message.get("content") or ""))
        if parsed is None:
            continue
        tool, arguments = parsed
        path = _argument_path(arguments)
        if path is None or _SKILL_MD.search(path) is None:
            continue
        if tool == "read":
            content = _next_tool_content(serialized, index)
            usage = DetectedSkillUsage.INJECTED
        elif tool == "write":
            content = _argument_text(arguments, "content")
            usage = DetectedSkillUsage.MODIFIED
        elif tool == "edit":
            content = next(
                (
                    value
                    for key in ("content", "new_content", "replacement", "replace")
                    if (value := _argument_text(arguments, key))
                ),
                "",
            )
            usage = DetectedSkillUsage.MODIFIED
        else:
            continue
        if not content:
            continue
        name = frontmatter_value(content, "name") or Path(path.replace("\\", "/")).parent.name or "skill"
        candidate = DetectedSkillCandidate(
            path=path,
            content=content,
            name=name,
            version_label=frontmatter_value(content, "version"),
            usage=usage,
        )
        key = path.replace("\\", "/").rsplit("/", 1)[0]
        previous = candidates.get(key)
        if previous is None or _USAGE_PRIORITY[usage] >= _USAGE_PRIORITY[previous.usage]:
            candidates[key] = candidate
    return list(candidates.values())


def _parse_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    match = _TOOL_CALL.match(content)
    if match is None:
        return None
    try:
        arguments = json.loads(match.group(2).strip() or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return match.group(1).strip().lower(), arguments


def _argument_path(arguments: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "filepath"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _argument_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value if isinstance(value, str) else ""


def _next_tool_content(messages: list[dict[str, Any]], index: int) -> str:
    if index + 1 >= len(messages) or messages[index + 1].get("role") != "tool":
        return ""
    content = messages[index + 1].get("content")
    return content if isinstance(content, str) else ""


__all__ = ["detect_openclaw_skill_candidates"]
