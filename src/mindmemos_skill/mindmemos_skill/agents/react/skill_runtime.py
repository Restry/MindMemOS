"""Skill runtimes supported by the OpenAI-compatible ReAct family."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from xml.sax.saxutils import escape

from ...typing import Skill, SkillBinding, SkillInjectionMode, Trajectory
from ..skill_runtime import SkillInjection, SkillRuntime
from .tool import Tool

_SKILL_TOOL_NAME = "Skill"


def _extract_loaded_skill_names(messages: list[dict[str, Any]]) -> set[str]:
    """Interpret successful results from the ReAct built-in Skill tool."""

    loaded: set[str] = set()
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != _SKILL_TOOL_NAME:
            continue
        content = message.get("content")
        if not isinstance(content, str) or content.startswith("Error:"):
            continue
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            continue
        name = result.get("name") if isinstance(result, dict) else None
        if isinstance(name, str) and name:
            loaded.add(name)
    return loaded


def _skill_catalog_suffix(skills: Mapping[str, Skill]) -> str:
    lines = ["<available_skills>"]
    for skill in skills.values():
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description or '')}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _skill_system_prompt_suffix(skills: Mapping[str, Skill]) -> str | None:
    snapshots = [(skill, skill.content.strip()) for skill in skills.values() if skill.content.strip()]
    if not snapshots:
        return None

    lines = ["<available_skills>"]
    for skill, content in snapshots:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description or '')}</description>",
                f"    <content>{escape(content)}</content>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _build_skill_tool(skills: Mapping[str, Skill]) -> Tool:
    def load_skill(skill: str) -> dict[str, Any]:
        selected = skills.get(skill)
        if selected is None:
            available = ", ".join(skills)
            raise ValueError(f"unknown Skill {skill!r}; available Skills: {available}")
        return {
            "name": selected.name,
            "version_id": selected.version_id,
            "content_hash": selected.content_hash,
            "blob": selected.blob,
            "resources": selected.resources,
        }

    return Tool(
        name=_SKILL_TOOL_NAME,
        description="Load one injected MindMemOS Skill version and return its persisted bundle.",
        parameters={
            "type": "object",
            "properties": {"skill": {"type": "string", "enum": list(skills)}},
            "required": ["skill"],
            "additionalProperties": False,
        },
        func=load_skill,
    )


class ReactSkillRuntime(SkillRuntime):
    """Inject and bind Skills according to one ReAct-supported mode."""

    supported_modes = frozenset({SkillInjectionMode.TOOL, SkillInjectionMode.SYSTEM_PROMPT})

    @contextmanager
    def inject(self, skills: list[Skill]) -> Iterator[SkillInjection]:
        by_name: dict[str, Skill] = {}
        for skill in skills:
            if skill.name in by_name:
                raise ValueError(f"duplicate injected Skill name: {skill.name!r}")
            by_name[skill.name] = skill

        injection = SkillInjection(mode=self.mode, skill_names=set(by_name))
        if by_name and self.mode is SkillInjectionMode.TOOL:
            injection.system_prompt_suffix = _skill_catalog_suffix(by_name)
            injection.tools.append(_build_skill_tool(by_name))
        elif by_name and self.mode is SkillInjectionMode.SYSTEM_PROMPT:
            injection.system_prompt_suffix = _skill_system_prompt_suffix(by_name)
        yield injection

    def bind(self, trajectory: Trajectory) -> list[SkillBinding]:
        loaded_names = (
            {skill.name for skill in trajectory.injected_skills}
            if self.mode is SkillInjectionMode.SYSTEM_PROMPT
            else _extract_loaded_skill_names(trajectory.events)
        )
        return self._build_bindings(trajectory, loaded_names)


__all__ = ["ReactSkillRuntime"]
