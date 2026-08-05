"""Read and reconstruct immutable UTF-8 Skill snapshots."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import SkillSnapshotError
from ..persistence import SkillRecord
from .bundle import compute_content_hash, deserialize_files, normalize_text
from .models import SkillSnapshot, SnapshotFile, SnapshotFileRole

_IGNORED_DIRECTORIES = frozenset({".git", "__pycache__"})
_IGNORED_FILES = frozenset({".DS_Store"})


def read_skill_snapshot(source_path: str | Path) -> SkillSnapshot:
    source = Path(source_path).expanduser()
    root = source.parent if source.is_file() and source.name == "SKILL.md" else source
    root = root.resolve()
    if not root.is_dir():
        raise SkillSnapshotError(f"Skill source does not exist or is not a directory: {root}")

    blob: dict[str, str] = {}
    resources: dict[str, str] = {}
    files: list[SnapshotFile] = []
    for path in _iter_files(root):
        relative = _relative_path(root, path)
        try:
            normalized = normalize_text(path.read_bytes().decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise SkillSnapshotError(f"binary files are not supported in Skill snapshots: {relative}") from exc
        raw = normalized.encode("utf-8")
        role = _file_role(relative)
        target = blob if role in {SnapshotFileRole.ALGORITHM, SnapshotFileRole.SCRIPT} else resources
        target[relative] = normalized
        files.append(
            SnapshotFile(
                path=relative,
                content_hash=hashlib.sha256(raw).hexdigest(),
                byte_size=len(raw),
                mode=stat.S_IMODE(path.stat().st_mode),
                media_type=mimetypes.guess_type(relative)[0],
                role=role,
            )
        )
    if "SKILL.md" not in blob:
        raise SkillSnapshotError(f"Skill source contains no SKILL.md: {root}")
    return _build_snapshot(blob=blob, resources=resources, files=files)


def snapshot_from_editor(content: str, inherited: SkillSnapshot) -> SkillSnapshot:
    normalized = normalize_text(content)
    blob = dict(inherited.blob)
    blob["SKILL.md"] = normalized
    files = [item.model_copy(deep=True) for item in inherited.files]
    skill_file = next((item for item in files if item.path == "SKILL.md"), None)
    if skill_file is None:
        raise SkillSnapshotError("inherited snapshot contains no SKILL.md")
    raw = normalized.encode("utf-8")
    files[files.index(skill_file)] = skill_file.model_copy(
        update={"content_hash": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)}
    )
    return _build_snapshot(blob=blob, resources=dict(inherited.resources), files=files)


def snapshot_from_record(record: SkillRecord) -> SkillSnapshot:
    blob = deserialize_files(record.blob)
    resources = deserialize_files(record.resources)
    metadata = record.metadata.get("snapshot")
    if not isinstance(metadata, dict):
        raise SkillSnapshotError(f"version {record.version_id} has no snapshot metadata")
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list):
        raise SkillSnapshotError(f"version {record.version_id} has invalid snapshot files")
    try:
        files = [SnapshotFile.model_validate(item) for item in raw_files]
    except ValueError as exc:
        raise SkillSnapshotError(f"version {record.version_id} has invalid snapshot metadata") from exc
    snapshot = _build_snapshot(blob=blob, resources=resources, files=files)
    expected_hash = metadata.get("local_snapshot_hash")
    if snapshot.content_hash != record.content_hash:
        raise SkillSnapshotError(f"version {record.version_id} content hash is corrupt")
    if snapshot.local_snapshot_hash != expected_hash:
        raise SkillSnapshotError(f"version {record.version_id} local snapshot hash is corrupt")
    return snapshot


def snapshot_metadata(snapshot: SkillSnapshot) -> dict[str, Any]:
    return {
        "local_snapshot_hash": snapshot.local_snapshot_hash,
        "files": [item.model_dump(mode="json") for item in snapshot.files],
    }


def validate_snapshot_path(path: str) -> str:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SkillSnapshotError(f"invalid snapshot-relative path: {path}")
    return candidate.as_posix()


def _build_snapshot(
    *,
    blob: dict[str, str],
    resources: dict[str, str],
    files: list[SnapshotFile],
) -> SkillSnapshot:
    normalized_blob = {validate_snapshot_path(path): normalize_text(text) for path, text in blob.items()}
    normalized_resources = {validate_snapshot_path(path): normalize_text(text) for path, text in resources.items()}
    if set(normalized_blob) & set(normalized_resources):
        raise SkillSnapshotError("snapshot paths may not appear in both blob and resources")
    expected_paths = set(normalized_blob) | set(normalized_resources)
    actual_paths = [validate_snapshot_path(item.path) for item in files]
    if len(actual_paths) != len(set(actual_paths)) or set(actual_paths) != expected_paths:
        raise SkillSnapshotError("snapshot file manifest does not match stored file content")
    normalized_files = sorted(files, key=lambda item: item.path)
    contents = {**normalized_blob, **normalized_resources}
    for item in normalized_files:
        raw = contents[item.path].encode("utf-8")
        if item.content_hash != hashlib.sha256(raw).hexdigest() or item.byte_size != len(raw):
            raise SkillSnapshotError(f"snapshot file metadata does not match content: {item.path}")
    content_hash = compute_content_hash(normalized_blob)
    canonical = json.dumps(
        {
            "content_hash": content_hash,
            "files": [
                {
                    "path": item.path,
                    "content_hash": item.content_hash,
                    "mode": item.mode,
                    "role": item.role.value,
                }
                for item in normalized_files
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return SkillSnapshot(
        blob=normalized_blob,
        resources=normalized_resources,
        files=normalized_files,
        content_hash=content_hash,
        local_snapshot_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _iter_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(name for name in directory_names if name not in _IGNORED_DIRECTORIES)
        for directory_name in directory_names:
            if (current / directory_name).is_symlink():
                raise SkillSnapshotError(f"symbolic links are not supported: {current / directory_name}")
        for file_name in sorted(file_names):
            if file_name in _IGNORED_FILES or file_name.endswith(".pyc"):
                continue
            path = current / file_name
            if path.is_symlink():
                raise SkillSnapshotError(f"symbolic links are not supported: {path}")
            if path.is_file():
                paths.append(path)
    return sorted(paths)


def _relative_path(root: Path, path: Path) -> str:
    try:
        return validate_snapshot_path(path.resolve(strict=True).relative_to(root).as_posix())
    except (OSError, ValueError) as exc:
        raise SkillSnapshotError(f"snapshot file escapes Skill root: {path}") from exc


def _file_role(path: str) -> SnapshotFileRole:
    if path == "SKILL.md":
        return SnapshotFileRole.ALGORITHM
    top_level = PurePosixPath(path).parts[0].lower()
    if top_level == "scripts":
        return SnapshotFileRole.SCRIPT
    if top_level == "references":
        return SnapshotFileRole.REFERENCE
    return SnapshotFileRole.RESOURCE


__all__ = [
    "read_skill_snapshot",
    "snapshot_from_editor",
    "snapshot_from_record",
    "snapshot_metadata",
    "validate_snapshot_path",
]
