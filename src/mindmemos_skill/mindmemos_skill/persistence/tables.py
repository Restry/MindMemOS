"""Business-owned table catalog for local Skill persistence.

Infra supplies only generic database mechanics. This module is the sole owner
of the physical mapping from persistence row models to logical tables.
"""

from __future__ import annotations

from ..infra.database import FieldSpec, FieldType, IndexSpec, SchemaMigration, TableRegistry, TableSpec

SKILL_TABLE = "skill_versions"
TRAJECTORY_TABLE = "trajectories"
ALGORITHM_LOG_TABLE = "algorithm_logs"
SKILL_FAMILY_STATE_TABLE = "skill_family_state"


def build_persistence_tables() -> TableRegistry:
    specs = (
        TableSpec(
            name=SKILL_TABLE,
            primary_key="version_id",
            fields=(
                _text("skill_id", nullable=False),
                _text("version_id", nullable=False),
                _text("cloud_skill_id"),
                _json("parent_version_ids", nullable=False, default=[]),
                _text("name", nullable=False),
                _text("description"),
                _text("alias"),
                _text("blob", nullable=False),
                _text("resources", nullable=False, default="{}"),
                _text("content_hash", nullable=False),
                _text("status", nullable=False),
                _text("version_label", nullable=False),
                _text("commit_message"),
                _json("metadata", nullable=False, default={}),
                _datetime("created_at", nullable=False),
                _text("origin", nullable=False),
            ),
            indexes=(
                IndexSpec(
                    name="skill_versions_label_uq",
                    fields=("skill_id", "version_label"),
                    unique=True,
                ),
                IndexSpec(name="skill_versions_hash_idx", fields=("content_hash",)),
            ),
            scope_scoped=False,
        ),
        TableSpec(
            name=SKILL_FAMILY_STATE_TABLE,
            primary_key="skill_id",
            fields=(
                _text("skill_id", nullable=False),
                _text("effective_version_id", nullable=False),
                _text("published_head_id"),
                _integer("cloud_revision"),
                _datetime("last_sync_at"),
                _json("pending_operations", nullable=False, default=[]),
                _datetime("created_at", nullable=False),
                _datetime("updated_at", nullable=False),
            ),
            indexes=(
                IndexSpec(name="skill_family_state_effective_idx", fields=("effective_version_id",)),
                IndexSpec(name="skill_family_state_published_idx", fields=("published_head_id",)),
            ),
            scope_scoped=False,
        ),
        TableSpec(
            name=TRAJECTORY_TABLE,
            primary_key="trajectory_id",
            fields=(
                _text("trajectory_id", nullable=False),
                _text("task_id", nullable=False),
                _text("rollout_id", nullable=False),
                _integer("attempt_no", nullable=False, default=0),
                _text("rollout_type", nullable=False),
                _text("task_instruction", nullable=False),
                _text("task_system_prompt"),
                _json("task_tags", nullable=False, default=[]),
                _json("task_metadata", nullable=False, default={}),
                _text("running_dir"),
                _json("env_metadata", nullable=False, default={}),
                _json("injected_skills", nullable=False, default=[]),
                _text("agent_type", nullable=False),
                _json("agent_profile", nullable=False, default={}),
                _text("status", nullable=False),
                _json("trajectory", nullable=False, default=[]),
                _json("skill_bindings", nullable=False, default=[]),
                _float("reward_score"),
                _text("reward_detail"),
                _json("reward_metadata", nullable=False, default={}),
                _datetime("started_at", nullable=False),
                _datetime("finished_at"),
                _integer("n_turn", nullable=False, default=0),
                _text("error_info"),
                _json("metadata", nullable=False, default={}),
            ),
            indexes=(
                IndexSpec(
                    name="trajectories_rollout_attempt_uq",
                    fields=("rollout_id", "attempt_no"),
                    unique=True,
                ),
                IndexSpec(name="trajectories_task_idx", fields=("task_id",)),
            ),
            scope_scoped=False,
        ),
        TableSpec(
            name=ALGORITHM_LOG_TABLE,
            primary_key="log_id",
            fields=(
                _text("log_id", nullable=False),
                _text("algorithm_name", nullable=False),
                _text("algorithm_version"),
                _text("component_name", nullable=False),
                _text("step_name", nullable=False),
                _text("status"),
                _json("payload", nullable=False, default={}),
                _datetime("created_at", nullable=False),
            ),
            indexes=(
                IndexSpec(
                    name="algorithm_logs_algorithm_created_idx",
                    fields=("algorithm_name", "created_at"),
                ),
            ),
            scope_scoped=False,
        ),
    )
    registry = TableRegistry(
        specs,
        migrations=(
            SchemaMigration(
                namespace="skill-persistence",
                version=1,
                name="create_fact_tables",
                tables=(SKILL_TABLE, TRAJECTORY_TABLE, ALGORITHM_LOG_TABLE),
            ),
            SchemaMigration(
                namespace="skill-persistence",
                version=2,
                name="create_skill_family_state",
                tables=(SKILL_FAMILY_STATE_TABLE,),
            ),
        ),
    )
    registry.freeze()
    return registry


def _text(name: str, *, nullable: bool = True, default=None) -> FieldSpec:
    return FieldSpec(name=name, field_type=FieldType.TEXT, nullable=nullable, default=default)


def _integer(name: str, *, nullable: bool = True, default=None) -> FieldSpec:
    return FieldSpec(name=name, field_type=FieldType.INTEGER, nullable=nullable, default=default)


def _float(name: str, *, nullable: bool = True, default=None) -> FieldSpec:
    return FieldSpec(name=name, field_type=FieldType.FLOAT, nullable=nullable, default=default)


def _datetime(name: str, *, nullable: bool = True, default=None) -> FieldSpec:
    return FieldSpec(name=name, field_type=FieldType.DATETIME, nullable=nullable, default=default)


def _json(name: str, *, nullable: bool = True, default=None) -> FieldSpec:
    return FieldSpec(name=name, field_type=FieldType.JSON, nullable=nullable, default=default)


__all__ = [
    "ALGORITHM_LOG_TABLE",
    "SKILL_FAMILY_STATE_TABLE",
    "SKILL_TABLE",
    "TRAJECTORY_TABLE",
    "build_persistence_tables",
]
