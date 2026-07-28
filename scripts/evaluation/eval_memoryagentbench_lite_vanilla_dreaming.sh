#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EVAL_CONFIG="${MINDMEMOS_EVAL_CONFIG:-config/mindmemos_eval/dreaming_evaluation_mab.example.yaml}"

: "${MINDMEMOS_EVAL_LLM_API_KEY:?set MINDMEMOS_EVAL_LLM_API_KEY for benchmark answer generation}"
: "${MINDMEMOS_EVAL_LLM_BASE_URL:?set MINDMEMOS_EVAL_LLM_BASE_URL for benchmark answer generation}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

PGVECTOR_SCHEMA="eval_mab_lite_vanilla_dreaming_${RUN_ID}" \
uv run --project src/mindmemos_lite \
  --with-editable src/mindmemos_eval \
  --with-editable src/mindmemos_sdk \
  python -m mindmemos_eval.cli memory \
  --benchmark-config "${EVAL_CONFIG}" \
  --benchmark-list memoryagentbench \
  --manifest-output "reports/memoryagentbench_lite_vanilla_dreaming_${RUN_ID}.jsonl" \
  --algorithm vanilla \
  --memory-connection-mode in_memory \
  --lite-config-path config/mindmemos_lite/dev.yaml \
  --lite-config-name dev \
  --no-lite-load-config-from-env \
  --lite-start-workers \
  --top-k 50 \
  --search-strategy fast \
  --no-rerank \
  --max-conv-concurrency 1 \
  --max-qa-concurrency 30 \
  --judge-runs 1 \
  --add \
  --dreaming-after-add \
  --score \
  --show-progress
