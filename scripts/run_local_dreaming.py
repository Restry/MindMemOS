#!/usr/bin/env python3
"""Run one bounded MindMemOS Dreaming cycle with locking and safe audit output."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
API_BASE = os.getenv("MINDMEMOS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_KEYS = Path(os.getenv("MINDMEMOS_API_KEYS", str(ROOT / "config/mindmemos/api_keys.yaml")))
USER_ID = os.getenv("MINDMEMOS_DREAMING_USER", "").strip()
LOCK_PATH = Path(os.path.expanduser(os.getenv("MINDMEMOS_DREAMING_LOCK", "~/.hermes/locks/mindmemos-dreaming.lock")))
AUDIT_PATH = Path(
    os.path.expanduser(os.getenv("MINDMEMOS_DREAMING_AUDIT", "~/.hermes/logs/mindmemos-dreaming-audit.jsonl"))
)


def build_request(user_id: str) -> dict[str, str]:
    return {"mode": "sync", "user_id": user_id}


def require_user_id(user_id: str) -> str:
    value = user_id.strip()
    if not value:
        raise RuntimeError("MINDMEMOS_DREAMING_USER must be set explicitly")
    return value


def load_api_key() -> str:
    config = yaml.safe_load(API_KEYS.read_text(encoding="utf-8")) or {}
    for item in config.get("api_keys") or []:
        if item.get("enabled") and item.get("memory_algorithm") == "vanilla" and item.get("api_key"):
            return str(item["api_key"])
    raise RuntimeError("No enabled vanilla API key")


def append_audit(entry: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(AUDIT_PATH, 0o600)


def post_dreaming(api_key: str) -> dict[str, Any]:
    user_id = require_user_id(USER_ID)
    request = urllib.request.Request(
        f"{API_BASE}/v1/memory/dreaming",
        data=json.dumps(build_request(user_id)).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Dreaming HTTP {exc.code}: {body[:500]}") from exc


def check_prerequisites() -> dict[str, Any]:
    user_id = require_user_id(USER_ID)
    api_key = load_api_key()
    request = urllib.request.Request(f"{API_BASE}/docs")
    with urllib.request.urlopen(request, timeout=10) as response:
        return {
            "ok": response.status == 200 and bool(api_key),
            "api_status": response.status,
            "user_id": user_id,
            "schedule_request": build_request(user_id),
        }


def run_once() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            append_audit({"started_at": started.isoformat(), "status": "skipped", "reason": "already_running"})
            return 0
        try:
            response = post_dreaming(load_api_key())
            code = str(response.get("code") or "")
            entry = {
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "ok" if code == "ok" else "error",
                "code": code,
                "message": str(response.get("message") or "")[:1000],
            }
            append_audit(entry)
            if code != "ok":
                raise RuntimeError(f"Dreaming returned code={code or 'empty'}")
            print(json.dumps(entry, ensure_ascii=False))
            return 0
        except Exception as exc:
            append_audit(
                {
                    "started_at": started.isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:1000],
                }
            )
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check_prerequisites(), ensure_ascii=False))
        return 0
    return run_once()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
