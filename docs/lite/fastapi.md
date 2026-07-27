# MindMemOS Lite FastAPI adapter

Lite has one application lifecycle: `mindmemos_lite.runtime.MindMemOS`. FastAPI is
an optional transport over that runtime, so Python embedding and HTTP hosting
initialize and close the same database, model clients, telemetry, and task
backend resources.

## Python runtime

The in-process entry point now uses Lite's dedicated Python namespace:

```python
from mindmemos_lite.runtime import MindMemOS

async with MindMemOS.from_config("config/mindmemos_lite/dev.yaml") as runtime:
    result = await runtime.memory.search(context, request)
```

The base package does not require FastAPI. Install the API dependencies only
when HTTP hosting is needed:

```bash
uv sync --project src/mindmemos_lite --extra api
```

## FastAPI

Create `config/mindmemos_lite/dev.yaml` and
`config/mindmemos_lite/api_keys.yaml`, then start the pgvector dependency and
the server:

```bash
cp config/mindmemos_lite/dev.example.yaml config/mindmemos_lite/dev.yaml
cp config/mindmemos_lite/api_keys.example.yaml config/mindmemos_lite/api_keys.yaml
make dev-lite-api
```

To start only the server when pgvector is already running:

```bash
make api-lite
```

The equivalent package command is:

```bash
uv run --project src/mindmemos_lite --extra api mindmemos-lite-api \
  --config config/mindmemos_lite/dev.yaml \
  --api-key-file config/mindmemos_lite/api_keys.yaml
```

Configuration can also be supplied with `MINDMEMOS_CONFIG_PATH` (or
`MINDMEMOS_CONFIG_NAME`) and `MINDMEMOS_API_KEY_FILE`. The server exposes
`/healthz`, `/docs`, and the `/v1/memory/*` routes. Public routes use:

```text
Authorization: Bearer <api_key>
```

Lite currently implements vanilla add, search, get, update, and delete.
Feedback and dreaming routes are present for API compatibility but return HTTP
501 until those application services are migrated. Async add additionally
requires an injected task backend; sync mode remains the default.
