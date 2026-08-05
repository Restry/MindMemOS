"""Composition helpers for the canonical local Skill state database."""

from __future__ import annotations

from pathlib import Path

from ..infra.database import DatabaseConfig, ScopedDatabase, bootstrap_database
from .tables import build_persistence_tables

DEFAULT_SKILL_DATABASE_PATH = Path("~/.mindmemos/skill/state.db").expanduser()


def default_skill_database_config(path: str | Path | None = None) -> DatabaseConfig:
    """Build the SQLite config without opening or mutating the filesystem."""

    resolved = DEFAULT_SKILL_DATABASE_PATH if path is None else Path(path).expanduser()
    return DatabaseConfig(
        provider="sqlite",
        options={"path": str(resolved)},
    )


async def bootstrap_skill_database(path: str | Path | None = None) -> ScopedDatabase:
    """Open the canonical Skill database and apply its explicit schema ledger."""

    return await bootstrap_database(default_skill_database_config(path), build_persistence_tables())


__all__ = [
    "DEFAULT_SKILL_DATABASE_PATH",
    "bootstrap_skill_database",
    "default_skill_database_config",
]
