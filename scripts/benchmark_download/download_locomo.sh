#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/datasets/locomo"
OUTPUT_PATH="${OUTPUT_DIR}/locomo10.json"

mkdir -p "${OUTPUT_DIR}"
curl -fL \
  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
  -o "${OUTPUT_PATH}"

echo "LoCoMo downloaded to ${OUTPUT_PATH}"
