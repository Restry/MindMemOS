#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/datasets/memoryagentbench"
OUTPUT_PATH="${OUTPUT_DIR}/conflict_resolution.jsonl"

mkdir -p "${OUTPUT_DIR}"
uv run --with datasets python -c \
  'import sys; from datasets import load_dataset; load_dataset("ai-hyz/MemoryAgentBench", split="Conflict_Resolution", revision="main").to_json(sys.argv[1], orient="records", lines=True, force_ascii=False)' \
  "${OUTPUT_PATH}"

echo "MemoryAgentBench Conflict Resolution downloaded to ${OUTPUT_PATH}"
