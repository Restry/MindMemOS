# MindMemOS Lite local dependencies

`dockers-lite` is the local dependency entry point for MindMemOS Lite. Each
database backend owns an isolated directory:

```text
dockers-lite/
├── compose.yaml
├── .env.example
└── backends/
    └── pgvector/
        └── compose.yaml
```

The root Compose file only aggregates backend definitions. Each backend uses a
Compose profile so future implementations can be started independently.
Compose starts PostgreSQL and creates its configured user and database. Python
owns extension, schema, table, and index initialization through
`ensure_database_schema`.

## Start pgvector

From the repository root:

```bash
cp dockers-lite/.env.example dockers-lite/.env
make dev-lite
```

`make dev-lite` currently starts only the pgvector dependency. It does not
start the MindMemOS Lite API. The equivalent Compose command is:

```bash
docker compose \
  --env-file dockers-lite/.env \
  -f dockers-lite/compose.yaml \
  --profile pgvector \
  up -d --wait
```

The default application DSN is:

```text
postgresql://postgres:postgres@127.0.0.1:5432/mindmemos
```

Export it before starting MindMemOS Lite:

```bash
export PGVECTOR_DSN='postgresql://postgres:postgres@127.0.0.1:5432/mindmemos'
```

After Python schema initialization, verify the installed extension:

```bash
docker compose \
  --env-file dockers-lite/.env \
  -f dockers-lite/compose.yaml \
  exec pgvector \
  psql -U postgres -d mindmemos -c \
  "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
```

Stop the service while preserving data:

```bash
docker compose \
  --env-file dockers-lite/.env \
  -f dockers-lite/compose.yaml \
  --profile pgvector \
  down
```

Use `down -v` only when the local PostgreSQL data volume should also be
deleted.

## Add another backend

1. Create `backends/<provider>/compose.yaml`.
2. Keep provider-specific infrastructure files inside that directory.
3. Give its services a `<provider>` Compose profile.
4. Add the provider Compose file to the root `include` list.
5. Document the provider DSN or endpoint in `.env.example`.

Do not place provider-specific services directly in the root Compose file, and
do not initialize application schemas from Compose.
