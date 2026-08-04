"""Shared skill workspace and usage helpers for agent implementations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

from ..typing import Skill, SkillBinding, SkillUsageType
from ._claude_messages import extract_used_skill_names


def _skill_to_markdown(skill: Skill) -> str:
    """Render a skill as a Claude-compatible ``SKILL.md`` document."""
    parts = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
        "---",
        "",
    ]
    if skill.content:
        parts.extend((skill.content, ""))
    return "\n".join(parts)


def prepare_skills_workspace(skills: list[Skill]) -> str | None:
    """Write skills into a temporary Claude skill workspace."""
    if not skills:
        return None

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    workspace = tempfile.mkdtemp(prefix=f"mindmemos_skills_{timestamp}_")
    skills_dir = os.path.join(workspace, ".claude", "skills")
    os.makedirs(skills_dir, exist_ok=True)

    for skill in skills:
        safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in skill.name)
        skill_dir = os.path.join(skills_dir, safe_name)
        os.makedirs(skill_dir, exist_ok=True)
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as file:
            file.write(_skill_to_markdown(skill))

        abs_workspace = os.path.abspath(workspace)
        file_refs: list[str] = []
        for rel_path, content in skill.linked_files.items():
            abs_path = os.path.abspath(os.path.join(skill_dir, rel_path))
            if not abs_path.startswith(abs_workspace + os.sep):
                continue
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as file:
                file.write(content)
            file_refs.append(f"- `{rel_path}` — 和 SKILL.md 在同一目录")
        if file_refs:
            with open(skill_path, "a", encoding="utf-8") as file:
                file.write("\n\n## 关联文件\n\n" + "\n".join(file_refs))

    return workspace


def build_skill_bindings(
    skills: list[Skill],
    trajectory_messages: list[dict[str, object]],
) -> list[SkillBinding]:
    """Build deterministic bindings for all injected skills."""
    used_names = set(extract_used_skill_names(trajectory_messages))
    return [
        SkillBinding(
            name=skill.name,
            content_hash=_skill_content_hash(skill),
            usage=SkillUsageType.INJECTED if skill.name in used_names else SkillUsageType.UNUSED,
        )
        for skill in skills
    ]


def _skill_content_hash(skill: Skill) -> str:
    """Hash the complete injected skill bundle deterministically."""
    bundle = {
        "SKILL.md": _skill_to_markdown(skill),
        **skill.linked_files,
    }
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["build_skill_bindings", "prepare_skills_workspace"]
