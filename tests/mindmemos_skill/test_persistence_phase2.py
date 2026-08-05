from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any

import pytest
from mindmemos_skill.infra.database import (
    DatabaseConfig,
    DatabaseScope,
    FieldSpec,
    FieldType,
    SchemaMigration,
    TableRegistry,
    TableSpec,
    bootstrap_database,
)
from mindmemos_skill.persistence import (
    DEFAULT_SKILL_DATABASE_PATH,
    SKILL_FAMILY_STATE_TABLE,
    SKILL_TABLE,
    SkillFamilyStateRecord,
    SkillRecord,
    bootstrap_skill_database,
    build_persistence_tables,
    default_skill_database_config,
    from_database_record,
    to_database_record,
)


def _skill(version_id: str = "version-1", version_label: str = "1.0.0") -> SkillRecord:
    return SkillRecord(
        skill_id="skill-1",
        version_id=version_id,
        name="research-brief",
        blob='{"SKILL.md":"# Research brief"}',
        content_hash=f"sha256:{version_id}",
        version_label=version_label,
    )


def _state(effective_version_id: str = "version-1") -> SkillFamilyStateRecord:
    now = datetime(2026, 8, 5, tzinfo=UTC)
    return SkillFamilyStateRecord(
        skill_id="skill-1",
        effective_version_id=effective_version_id,
        pending_operations=[
            {
                "operation_id": "push-skill-1-version-1",
                "kind": "push",
                "status": "pending",
            }
        ],
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_persistence_migrations_adopt_legacy_fact_catalog_and_add_family_state(tmp_path) -> None:
    path = tmp_path / "state.db"
    current = build_persistence_tables()
    legacy = TableRegistry(spec for spec in current.specs if spec.name != SKILL_FAMILY_STATE_TABLE)
    legacy.freeze()
    database = await bootstrap_database(DatabaseConfig(options={"path": str(path)}), legacy)
    await database.close()

    migrated = await bootstrap_skill_database(path)
    await migrated.close()

    with sqlite3.connect(path) as connection:
        migrations = connection.execute(
            "SELECT namespace, version, name FROM __mindmemos_migrations ORDER BY version"
        ).fetchall()
        family_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'skill_family_state'"
        ).fetchone()

    assert migrations == [
        ("skill-persistence", 1, "create_fact_tables"),
        ("skill-persistence", 2, "create_skill_family_state"),
    ]
    assert family_table == ("skill_family_state",)


@pytest.mark.asyncio
async def test_failed_schema_migration_rolls_back_its_catalog_changes(tmp_path) -> None:
    path = tmp_path / "state.db"
    first = TableSpec(
        name="first_table",
        primary_key="record_id",
        fields=(FieldSpec(name="record_id", field_type=FieldType.TEXT, nullable=False),),
        scope_scoped=False,
    )
    omitted = TableSpec(
        name="omitted_table",
        primary_key="record_id",
        fields=(FieldSpec(name="record_id", field_type=FieldType.TEXT, nullable=False),),
        scope_scoped=False,
    )
    invalid_catalog = TableRegistry(
        (first, omitted),
        migrations=(
            SchemaMigration(
                namespace="rollback-test",
                version=1,
                name="incomplete_catalog",
                tables=("first_table",),
            ),
        ),
    )
    invalid_catalog.freeze()

    with pytest.raises(RuntimeError, match="missing an explicit schema migration"):
        await bootstrap_database(DatabaseConfig(options={"path": str(path)}), invalid_catalog)

    with sqlite3.connect(path) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert tables == []


@pytest.mark.asyncio
async def test_transaction_commits_version_family_pointer_and_outbox_together(tmp_path) -> None:
    database = await bootstrap_skill_database(tmp_path / "state.db")
    skill = _skill()
    state = _state()

    async with database.transaction() as unit_of_work:
        await unit_of_work.upsert_records(SKILL_TABLE, (to_database_record(skill),))
        await unit_of_work.upsert_records(SKILL_FAMILY_STATE_TABLE, (to_database_record(state),))

    stored_skill = await database.get_records(SKILL_TABLE, DatabaseScope(), (skill.version_id,))
    stored_state = await database.get_records(SKILL_FAMILY_STATE_TABLE, DatabaseScope(), (state.skill_id,))
    assert from_database_record(stored_skill[0], SkillRecord) == skill
    assert from_database_record(stored_state[0], SkillFamilyStateRecord) == state
    await database.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_version_family_pointer_and_outbox_together(tmp_path) -> None:
    database = await bootstrap_skill_database(tmp_path / "state.db")
    skill = _skill()
    state = _state()

    with pytest.raises(RuntimeError, match="abort transaction"):
        async with database.transaction() as unit_of_work:
            await unit_of_work.upsert_records(SKILL_TABLE, (to_database_record(skill),))
            await unit_of_work.upsert_records(SKILL_FAMILY_STATE_TABLE, (to_database_record(state),))
            raise RuntimeError("abort transaction")

    assert await database.get_records(SKILL_TABLE, DatabaseScope(), (skill.version_id,)) == []
    assert await database.get_records(SKILL_FAMILY_STATE_TABLE, DatabaseScope(), (state.skill_id,)) == []
    await database.close()


@pytest.mark.asyncio
async def test_compare_and_swap_updates_family_pointer_only_for_expected_value(tmp_path) -> None:
    database = await bootstrap_skill_database(tmp_path / "state.db")
    await database.upsert_records(SKILL_FAMILY_STATE_TABLE, (to_database_record(_state()),))

    won = await database.compare_and_swap_record(
        SKILL_FAMILY_STATE_TABLE,
        DatabaseScope(),
        "skill-1",
        expected={"effective_version_id": "version-1"},
        changes={"effective_version_id": "version-2"},
    )
    stale = await database.compare_and_swap_record(
        SKILL_FAMILY_STATE_TABLE,
        DatabaseScope(),
        "skill-1",
        expected={"effective_version_id": "version-1"},
        changes={"effective_version_id": "version-3"},
    )

    stored = await database.get_records(SKILL_FAMILY_STATE_TABLE, DatabaseScope(), ("skill-1",))
    assert won is True
    assert stale is False
    assert stored[0].payload["effective_version_id"] == "version-2"
    assert stored[0].payload["pending_operations"] == _state().pending_operations
    await database.close()


def _cas_process(
    path: str,
    replacement: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    async def run() -> None:
        database = await bootstrap_skill_database(path)
        ready.put(replacement)
        start.wait(timeout=10)
        try:
            changed = await database.compare_and_swap_record(
                SKILL_FAMILY_STATE_TABLE,
                DatabaseScope(),
                "skill-1",
                expected={"effective_version_id": "version-1"},
                changes={"effective_version_id": replacement},
            )
            results.put((replacement, changed, None))
        except BaseException as exc:
            results.put((replacement, False, repr(exc)))
            raise
        finally:
            await database.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_compare_and_swap_is_safe_across_processes(tmp_path) -> None:
    path = tmp_path / "state.db"
    database = await bootstrap_skill_database(path)
    await database.upsert_records(SKILL_FAMILY_STATE_TABLE, (to_database_record(_state()),))
    await database.close()

    context = multiprocessing.get_context("spawn")
    ready: Queue[str] = context.Queue()
    start = context.Event()
    results: Queue[tuple[str, bool, str | None]] = context.Queue()
    processes = [
        context.Process(target=_cas_process, args=(str(path), replacement, ready, start, results))
        for replacement in ("version-2", "version-3")
    ]
    for process in processes:
        process.start()
    assert {ready.get(timeout=15), ready.get(timeout=15)} == {"version-2", "version-3"}
    start.set()
    outcomes = [results.get(timeout=15), results.get(timeout=15)]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(changed for _, changed, error in outcomes if error is None) == [False, True]
    reopened = await bootstrap_skill_database(path)
    stored = await reopened.get_records(SKILL_FAMILY_STATE_TABLE, DatabaseScope(), ("skill-1",))
    assert stored[0].payload["effective_version_id"] in {"version-2", "version-3"}
    await reopened.close()


def test_default_skill_database_path_is_canonical_and_side_effect_free() -> None:
    assert DEFAULT_SKILL_DATABASE_PATH == Path.home() / ".mindmemos" / "skill" / "state.db"
    assert default_skill_database_config().options["path"] == str(DEFAULT_SKILL_DATABASE_PATH)
