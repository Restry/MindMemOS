# mindmemos-skill

Skill components for MindMemOS.

This package is the home for reusable skill definitions, runtime helpers, and
related integrations. It owns a backend-neutral storage infra layer and does
not depend on `mindmemos_sdk`.

## Installation

The core package contains the management contracts and SQLite-backed
persistence infrastructure without installing model or PostgreSQL clients:

```bash
pip install mindmemos-skill
```

Install only the runtime capabilities an application uses:

```bash
pip install 'mindmemos-skill[llm]'
pip install 'mindmemos-skill[pgvector]'
pip install 'mindmemos-skill[claude-sdk]'
pip install 'mindmemos-skill[alfworld]'
```

## Storage infra

Storage is split into two independent capabilities:

- `mindmemos_skill.infra.database` stores core structured persistence data.
  It has its own backend registry and ships SQLite by default.
- `mindmemos_skill.infra.vector_store` is an optional algorithm index. It has a
  separate backend registry and ships PostgreSQL + pgvector by default.

SQLite is not a VectorStore, and PGVector is not selected as the core database.
Custom providers are registered independently in the capability they
implement. Infra owns only generic records, schemas, filtering, and adapter
contracts; `mindmemos_skill.persistence` owns the Skill business table catalog.

Bootstrap the core persistence database through `DatabaseConfig`:

```python
from mindmemos_skill.infra.database import (
    DatabaseConfig,
    FieldSpec,
    FieldType,
    TableRegistry,
    TableSpec,
    bootstrap_database,
)

tables = TableRegistry(
    (
        TableSpec(
            name="runtime_logs",
            primary_key="log_id",
            fields=(FieldSpec(name="message", field_type=FieldType.TEXT, nullable=False),),
        ),
    )
)
tables.freeze()

database = await bootstrap_database(
    DatabaseConfig(provider="sqlite", options={"path": ".mindmemos/skill.db"}),
    tables,
)
```

Change `provider` and `options` to use another registered structured database.
Algorithms that need similarity search configure
`infra.vector_store.BackendConfig` separately, so core persistence remains
usable without a vector database.

For Skill persistence, use the business-owned catalog and canonical default
path (`~/.mindmemos/skill/state.db`):

```python
from mindmemos_skill.persistence import bootstrap_skill_database

database = await bootstrap_skill_database()
async with database.transaction() as unit_of_work:
    await unit_of_work.upsert_records("skill_versions", version_records)
    await unit_of_work.upsert_records("skill_family_state", (family_state_record,))
```

The SQLite backend records ordered schema migrations, rolls the unit of work
back on errors, and exposes atomic `compare_and_swap_record(...)` for mutable
family pointers. Use the transaction-bound `unit_of_work` inside the context;
do not call the outer `database` object until the context exits.

## Standalone local management

`mindmemos_skill.management` owns the local management rules and can run
without the SDK or a cloud connection. `LocalSkillManager.open()` uses the
canonical SQLite database unless a test or embedding application supplies a
different path:

```python
from mindmemos_skill.management import (
    ExportSkillRequest,
    LocalSkillManager,
    PublishSkillRequest,
    RegisterSkillRequest,
)

manager = await LocalSkillManager.open()
registered = await manager.register(
    RegisterSkillRequest(
        source_path="./my-skill",
        alias="my-skill",
        version_label="1.0.0",
    )
)
candidate = await manager.publish(
    PublishSkillRequest(
        skill_ref=registered.skill_id,
        source_path="./my-skill-next",
        version_label="1.1.0",
    )
)
await manager.set_effective_version(
    registered.skill_id,
    candidate.version_id,
    expected_version_id=registered.version_id,
)
await manager.export(
    ExportSkillRequest(skill_ref=registered.skill_id, target_path="./exported-skill")
)
await manager.close()
```

Registration and publication persist an immutable version, family pointer, and
stable pending push operation in one transaction. Parent versions must already
belong to the same family, version labels are unique and monotonically ordered
as integer triples, and effective-pointer changes use compare-and-set. Export
restores the complete UTF-8 snapshot and preserves files it does not manage;
if replacement fails partway through, overwritten files are restored.

The migrated detector is deliberately agent-family-specific:
`detect_openclaw_skill_candidates(...)` recognizes OpenClaw text tool-call
evidence only. Claude SDK and other agents keep separate evidence parsers.

## Environment registry

Built-in benchmark environments are selected by name. `livemath` and the
lean-history ALFWorld protocol registered as `alfworld` are currently shipped:

```python
from mindmemos_skill.envs import get_env

env = get_env(name=env_name, config=env_params)
```

Future trainers should pass their configured `env_name` and `env_params`
through this factory rather than importing benchmark classes. Packages outside
MindMemOS can participate in the same selection path:

```python
from mindmemos_skill.envs import BaseEnv
from mindmemos_skill.registry import register

@register(type="env", name="my_benchmark")
class MyBenchmarkEnv(BaseEnv):
    ...
```

## Development

From the repository root:

```bash
uv sync
```

The import package is `mindmemos_skill`.
