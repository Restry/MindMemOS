"""Temporary Phase 0 guards for the SDK Skill-management migration boundary."""

from __future__ import annotations

import ast
from pathlib import Path

SDK_ROOT = Path("src/mindmemos_sdk/mindmemos_sdk")

# These are known migration debt, not endorsed extension points.  Later phases
# shrink this allowlist as CLI/UI calls move behind the SDK facade.
DIRECT_REPOSITORY_ACCESS_ALLOWLIST = {
    SDK_ROOT / "cli.py",
    SDK_ROOT / "skills/manager.py",
    SDK_ROOT / "ui/server.py",
    SDK_ROOT / "ui/skill_service.py",
}

LOCAL_REPOSITORY_IMPORT_ALLOWLIST = {
    SDK_ROOT / "skills/__init__.py",
    SDK_ROOT / "skills/manager.py",
    SDK_ROOT / "ui/server.py",
}


def test_no_new_sdk_module_reaches_through_skill_manager_to_local_repository() -> None:
    violations: list[str] = []
    for path in SDK_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "local_repository":
                if path not in DIRECT_REPOSITORY_ACCESS_ALLOWLIST:
                    violations.append(f"{path}:{node.lineno}")
    assert violations == []


def test_no_new_sdk_module_imports_local_skill_repository() -> None:
    violations: list[str] = []
    for path in SDK_ROOT.rglob("*.py"):
        if path.name == "local_repository.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "LocalSkillRepository" for alias in node.names
            ):
                if path not in LOCAL_REPOSITORY_IMPORT_ALLOWLIST:
                    violations.append(f"{path}:{node.lineno}")
    assert violations == []
