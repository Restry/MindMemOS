#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EVAL_CONFIG="${MINDMEMOS_EVAL_CONFIG:-config/mindmemos_eval/memory_evaluation_personamem.example.yaml}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

PGVECTOR_SCHEMA="eval_personamem_lite_vanilla_${RUN_ID}" \
uv run --project src/mindmemos_lite \
  --with-editable src/mindmemos_eval \
  --with-editable src/mindmemos_sdk \
  python -m mindmemos_eval.cli memory \
  --benchmark-config "${EVAL_CONFIG}" \
  --benchmark-list personamem \
  --manifest-output "reports/personamem_lite_vanilla/personamem_lite_vanilla_${RUN_ID}.jsonl" \
  --algorithm vanilla \
  --memory-connection-mode in_memory \
  --lite-config-path config/mindmemos_lite/dev.yaml \
  --lite-config-name dev \
  --no-lite-load-config-from-env \
  --lite-start-workers \
  "$@"
