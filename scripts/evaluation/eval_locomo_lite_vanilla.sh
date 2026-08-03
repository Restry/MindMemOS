#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EVAL_CONFIG="${MINDMEMOS_EVAL_CONFIG:-config/mindmemos_eval/memory_evaluation_locomo.example.yaml}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

PGVECTOR_SCHEMA="eval_locomo_lite_vanilla_${RUN_ID}" \
uv run --project src/mindmemos_lite \
  --with-editable src/mindmemos_eval \
  --with-editable src/mindmemos_sdk \
  python -m mindmemos_eval.cli memory \
  --benchmark-config "${EVAL_CONFIG}" \
  --benchmark-list locomo \
  --manifest-output "reports/locomo_lite_vanilla/locomo_lite_vanilla_${RUN_ID}.jsonl" \
  --algorithm vanilla \
  --memory-connection-mode in_memory \
  --lite-config-path config/mindmemos_lite/dev.yaml \
  --lite-config-name dev \
  --no-lite-load-config-from-env \
  --lite-start-workers \
  --top-k 50 \
  --search-strategy fast \
  --no-rerank \
  --max-conv-concurrency 10 \
  --max-qa-concurrency 30 \
  --max-search-concurrency 30 \
  --max-score-concurrency 30 \
  --judge-runs 1 \
  --add \
  --score \
  --show-progress
