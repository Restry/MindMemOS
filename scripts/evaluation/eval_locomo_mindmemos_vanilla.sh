#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

EVAL_CONFIG="${MINDMEMOS_EVAL_CONFIG:-config/mindmemos_eval/memory_evaluation_locomo.example.yaml}"

uv run python -m mindmemos_eval.cli memory \
  --benchmark-config "${EVAL_CONFIG}" \
  --benchmark-list locomo \
  --manifest-output reports/locomo_mindmemos_vanilla.jsonl \
  --reuse-api-key config/mindmemos/api_keys.yaml \
  --algorithm vanilla \
  --memory-connection-mode http \
  --base-url http://127.0.0.1:8000 \
  --timeout-seconds 1800 \
  --top-k 50 \
  --search-strategy fast \
  --no-rerank \
  --max-conv-concurrency 2 \
  --max-qa-concurrency 4 \
  --max-search-concurrency 4 \
  --max-score-concurrency 2 \
  --judge-runs 1 \
  --add \
  --score \
  --show-progress \
  --qdrant-url http://127.0.0.1:6333 \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --neo4j-username neo4j \
  --neo4j-password mindmemos_dev_password \
  --neo4j-database neo4j \
  --server-config config/mindmemos/dev.example.yaml
