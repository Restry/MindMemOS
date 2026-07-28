#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/datasets/personamem"
QUESTIONS_PATH="${OUTPUT_DIR}/questions_32k.csv"
CONTEXTS_PATH="${OUTPUT_DIR}/shared_contexts_32k.jsonl"

mkdir -p "${OUTPUT_DIR}"
curl -fL \
  "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v1/resolve/main/questions_32k.csv?download=true" \
  -o "${QUESTIONS_PATH}"
curl -fL \
  "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v1/resolve/main/shared_contexts_32k.jsonl?download=true" \
  -o "${CONTEXTS_PATH}"

echo "PersonaMem-32K downloaded to ${OUTPUT_DIR}/"
