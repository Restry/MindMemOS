# mindmemos-skill

Skill components for MindMemOS.

This package is the home for reusable skill definitions, runtime helpers, and
related integrations. It owns a backend-neutral storage infra layer and does
not depend on `mindmemos_sdk`.

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

## Development

From the repository root:

```bash
uv sync
```

The import package is `mindmemos_skill`.
