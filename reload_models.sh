#!/bin/bash
set -euo pipefail

uid="$(id -u)"
api_label="gui/${uid}/com.leway.mindmemos.api"
mcp_label="gui/${uid}/com.leway.mindmemos.mcp"

launchctl kickstart -k "$api_label"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
 done
curl -fsS --max-time 5 http://127.0.0.1:8000/healthz >/dev/null

launchctl kickstart -k "$mcp_label"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8765/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
 done
curl -fsS --max-time 5 http://127.0.0.1:8765/health >/dev/null
printf 'api=ok mcp=ok\n'
