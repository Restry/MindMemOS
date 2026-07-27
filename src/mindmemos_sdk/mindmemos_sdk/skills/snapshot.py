"""Complete local Skill snapshot ingestion and hashing.

The cloud-visible algorithm bundle and private linked files are deliberately
separated here. Callers may upload ``snapshot.content`` but must never serialize
``snapshot.file_contents`` or ``snapshot.local_snapshot_hash`` into cloud DTOs.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath

from ..errors import SkillSnapshotError
from .bundle import compute_content_hash, read_local_bundle, resolve_skill_dir, serialize_bundle
from .models import LocalSkillFileEntry, LocalSkillFileRole, LocalSkillSnapshot

_IGNORED_DIRECTORY_NAMES = frozenset({".git", "__pycache__"})
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})


def read_local_snapshot(source_path: str | os.PathLike[str]) -> LocalSkillSnapshot:
    """Read one external Skill directory exactly once into a validated snapshot."""

    root = resolve_skill_dir(source_path).expanduser().resolve()
    if not root.is_dir():
        raise SkillSnapshotError(f"skill source does not exist or is not a directory: {root}")

    bundle_files = read_local_bundle(root)
    content = serialize_bundle(bundle_files)
    content_hash = compute_content_hash(bundle_files)
    file_contents: dict[str, str] = {}
    entries: list[LocalSkillFileEntry] = []

    for path in _iter_snapshot_files(root):
        relative = _validated_relative_path(root, path)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillSnapshotError(
                f"binary linked file is not supported in the UTF-8 snapshot format: {relative}"
            ) from exc
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized_bytes = normalized.encode("utf-8")
        blob_hash = hashlib.sha256(normalized_bytes).hexdigest()
        role = _file_role(relative)
        file_contents[relative] = normalized
        entries.append(
            LocalSkillFileEntry(
                path=relative,
                blob_hash=blob_hash,
                byte_size=len(normalized_bytes),
                media_type=mimetypes.guess_type(relative)[0],
                mode=stat.S_IMODE(path.stat().st_mode),
                role=role,
            )
        )

    if "SKILL.md" not in file_contents:
        raise SkillSnapshotError(f"skill source contains no SKILL.md: {root}")

    entries.sort(key=lambda item: item.path)
    local_snapshot_hash = compute_local_snapshot_hash(content_hash, entries)
    return LocalSkillSnapshot(
        content=content,
        content_hash=content_hash,
        local_snapshot_hash=local_snapshot_hash,
        files=entries,
        file_contents=file_contents,
    )


def snapshot_from_editor(
    *,
    content: str,
    inherited_snapshot: LocalSkillSnapshot,
) -> LocalSkillSnapshot:
    """Create an editor snapshot by replacing ``SKILL.md`` and inheriting private files."""

    file_contents = dict(inherited_snapshot.file_contents)
    file_contents["SKILL.md"] = content.replace("\r\n", "\n").replace("\r", "\n")
    entries: list[LocalSkillFileEntry] = []
    for previous in inherited_snapshot.files:
        path = previous.path
        text = file_contents[path]
        raw = text.encode("utf-8")
        entries.append(
            previous.model_copy(
                update={
                    "blob_hash": hashlib.sha256(raw).hexdigest(),
                    "byte_size": len(raw),
                }
            )
        )
    bundle = {"SKILL.md": file_contents["SKILL.md"]}
    canonical_content = serialize_bundle(bundle)
    content_hash = compute_content_hash(bundle)
    entries.sort(key=lambda item: item.path)
    return LocalSkillSnapshot(
        content=canonical_content,
        content_hash=content_hash,
        local_snapshot_hash=compute_local_snapshot_hash(content_hash, entries),
        files=entries,
        file_contents=file_contents,
    )


def snapshot_from_cloud_content(
    *,
    content: str,
    inherited_snapshot: LocalSkillSnapshot | None = None,
) -> LocalSkillSnapshot:
    """Materialize cloud algorithm content while inheriting only local private files."""

    from .bundle import bundle_files_from_content

    algorithm_text = bundle_files_from_content(content)["SKILL.md"]
    inherited_entries = {
        item.path: item
        for item in (inherited_snapshot.files if inherited_snapshot is not None else [])
        if item.role != LocalSkillFileRole.ALGORITHM
    }
    file_contents = (
        dict(inherited_snapshot.linked_files)
        if inherited_snapshot is not None
        else {}
    )
    file_contents["SKILL.md"] = algorithm_text
    algorithm_raw = algorithm_text.encode("utf-8")
    entries = [
        LocalSkillFileEntry(
            path="SKILL.md",
            blob_hash=hashlib.sha256(algorithm_raw).hexdigest(),
            byte_size=len(algorithm_raw),
            media_type="text/markdown",
            mode=0o644,
            role=LocalSkillFileRole.ALGORITHM,
        ),
        *[inherited_entries[path] for path in sorted(inherited_entries)],
    ]
    bundle = {"SKILL.md": algorithm_text}
    canonical_content = serialize_bundle(bundle)
    content_hash = compute_content_hash(bundle)
    entries.sort(key=lambda item: item.path)
    return LocalSkillSnapshot(
        content=canonical_content,
        content_hash=content_hash,
        local_snapshot_hash=compute_local_snapshot_hash(content_hash, entries),
        files=entries,
        file_contents=file_contents,
    )


def compute_local_snapshot_hash(content_hash: str, files: list[LocalSkillFileEntry]) -> str:
    """Hash the canonical complete local snapshot manifest."""

    algorithm_files = []
    linked_files = []
    for item in sorted(files, key=lambda entry: entry.path):
        target = algorithm_files if item.role == LocalSkillFileRole.ALGORITHM else linked_files
        target.append(
            {
                "path": item.path,
                "blob_hash": item.blob_hash,
                "mode": item.mode,
                "role": item.role.value,
            }
        )
    canonical = json.dumps(
        {
            "content_hash": content_hash,
            "algorithm_files": algorithm_files,
            "linked_files": linked_files,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_snapshot_path(path: str) -> str:
    """Validate and normalize one snapshot-relative POSIX path."""

    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SkillSnapshotError(f"invalid snapshot-relative path: {path}")
    return candidate.as_posix()


def _iter_snapshot_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name for name in directory_names if name not in _IGNORED_DIRECTORY_NAMES
        )
        for directory_name in directory_names:
            directory_path = current / directory_name
            if directory_path.is_symlink():
                raise SkillSnapshotError(f"symbolic links are not supported in Skill snapshots: {directory_path}")
        for file_name in sorted(file_names):
            if file_name in _IGNORED_FILE_NAMES or file_name.endswith(".pyc"):
                continue
            path = current / file_name
            if path.is_symlink():
                raise SkillSnapshotError(f"symbolic links are not supported in Skill snapshots: {path}")
            if path.is_file():
                paths.append(path)
    return sorted(paths)


def _validated_relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise SkillSnapshotError(f"snapshot file escapes Skill root: {path}") from exc
    return validate_snapshot_path(relative.as_posix())


def _file_role(path: str) -> LocalSkillFileRole:
    if path == "SKILL.md":
        return LocalSkillFileRole.ALGORITHM
    first = PurePosixPath(path).parts[0].lower()
    if first == "references":
        return LocalSkillFileRole.REFERENCE
    if first == "scripts":
        return LocalSkillFileRole.SCRIPT
    return LocalSkillFileRole.ASSET
