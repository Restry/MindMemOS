"""Canonical serialization and hashing for complete local Skill snapshots."""

from __future__ import annotations

import hashlib
import json
import re

from ..errors import SkillConflictError, SkillSnapshotError

_VERSION_LABEL = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def normalize_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def serialize_files(files: dict[str, str]) -> str:
    return json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_files(payload: str) -> dict[str, str]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SkillSnapshotError("serialized Skill files are not valid JSON") from exc
    if not isinstance(value, dict) or any(
        not isinstance(path, str) or not isinstance(text, str) for path, text in value.items()
    ):
        raise SkillSnapshotError("serialized Skill files must map paths to text")
    return value


def compute_content_hash(blob: dict[str, str]) -> str:
    return hashlib.sha256(serialize_files(blob).encode("utf-8")).hexdigest()


def parse_version_label(value: str) -> tuple[int, int, int]:
    match = _VERSION_LABEL.fullmatch(value)
    if match is None:
        raise SkillConflictError(f"invalid version label {value!r}; expected x.y.z")
    return tuple(int(part) for part in match.groups())


def next_version_label(labels: list[str]) -> str:
    if not labels:
        return "0.1.0"
    major, minor, patch = max(parse_version_label(label) for label in labels)
    return f"{major}.{minor}.{patch + 1}"


def frontmatter_value(content: str, field: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(field)}\s*:\s*[\"']?([^\"'\n#]+)", content, re.MULTILINE)
    return match.group(1).strip() if match else None


__all__ = [
    "compute_content_hash",
    "deserialize_files",
    "frontmatter_value",
    "next_version_label",
    "normalize_text",
    "parse_version_label",
    "serialize_files",
]
