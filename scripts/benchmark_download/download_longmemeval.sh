#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/datasets/longmemeval"
OUTPUT_PATH="${OUTPUT_DIR}/longmemeval_s_cleaned.json"

mkdir -p "${OUTPUT_DIR}"
curl -fL \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o "${OUTPUT_PATH}"

echo "LongMemEval-S downloaded to ${OUTPUT_PATH}"
