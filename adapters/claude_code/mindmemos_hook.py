#!/usr/bin/env python3
"""Claude Code UserPromptSubmit/Stop adapter for durable MindMemOS ingestion."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
COMMON = HERE.parent / "python"
if not (HERE / "mindmemos_ingest_client.py").exists() and COMMON.exists():
    sys.path.insert(0, str(COMMON))
else:
    sys.path.insert(0, str(HERE))

from mindmemos_ingest_client import DurableIngestClient, read_key_file, stable_event_id  # noqa: E402

DEFAULT_CONFIG = Path(os.path.expanduser(os.getenv("MINDMEMOS_CLAUDE_CONFIG", "~/.config/mindmemos/claude-code.json")))


def _load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("content") or value.get("text") or value.get("message"))
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "output_text", "input_text"):
            text = part.get("text") or part.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _transcript_messages(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    messages: list[dict[str, Any]] = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(message, dict):
                    message = entry if isinstance(entry, dict) else {}
                role = message.get("role") or entry.get("role") or entry.get("type")
                if role not in ("user", "assistant"):
                    continue
                content = _text(message.get("content"))
                if not content:
                    continue
                messages.append(
                    {
                        "role": role,
                        "content": content,
                        "id": str(message.get("id") or entry.get("uuid") or entry.get("id") or len(messages)),
                        "timestamp": entry.get("timestamp") or message.get("timestamp"),
                    }
                )
    except OSError:
        return []
    return messages


def _latest_pair(path: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    messages = _transcript_messages(path)
    assistant_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index]["role"] == "assistant"),
        None,
    )
    if assistant_index is None:
        return None, None
    user = next(
        (messages[index] for index in range(assistant_index - 1, -1, -1) if messages[index]["role"] == "user"),
        None,
    )
    return user, messages[assistant_index]


class PromptStore:
    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    prompt_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    transcript_path TEXT,
                    submitted_at REAL NOT NULL,
                    completed_event_id TEXT,
                    assistant_message TEXT,
                    UNIQUE(session_id, prompt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_prompts_session
                    ON prompts(session_id, id DESC);
                """
            )
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 3000")
        return connection

    def remember_prompt(self, session_id: str, prompt_id: str, prompt: str, transcript_path: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO prompts(
                    session_id, prompt_id, prompt, transcript_path, submitted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, prompt_id, prompt, transcript_path, time.time()),
            )
            connection.execute(
                "DELETE FROM prompts WHERE id IN (SELECT id FROM prompts ORDER BY id DESC LIMIT -1 OFFSET 5000)"
            )

    def latest(self, session_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM prompts WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()

    def complete(self, row_id: int, event_id: str, assistant_message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE prompts
                   SET completed_event_id = ?, assistant_message = ?
                 WHERE id = ?
                """,
                (event_id, assistant_message, row_id),
            )


def _prompt_id(event: dict[str, Any], prompt: str, transcript_path: str | None) -> str:
    explicit = event.get("prompt_id") or event.get("message_id")
    if explicit:
        return str(explicit)
    transcript_size = 0
    if transcript_path:
        try:
            transcript_size = os.path.getsize(os.path.expanduser(transcript_path))
        except OSError:
            pass
    return stable_event_id(
        "prompt",
        event.get("session_id"),
        prompt,
        transcript_path,
        transcript_size,
    )


def _assistant_with_lag(event: dict[str, Any], transcript_path: str | None) -> tuple[str, dict[str, Any] | None]:
    direct = _text(event.get("last_assistant_message"))
    if direct:
        return direct, None
    latest: dict[str, Any] | None = None
    for _ in range(6):
        _user, latest = _latest_pair(transcript_path)
        if latest and latest.get("content"):
            return str(latest["content"]), latest
        time.sleep(0.1)
    return "", latest


def handle_event(event: dict[str, Any], config: dict[str, Any] | None = None) -> str | None:
    cfg = config or _load_config()
    service_url = str(cfg.get("service_url") or "http://127.0.0.1:8765")
    key = os.getenv("MINDMEMOS_INGEST_KEY") or read_key_file(cfg.get("key_file"))
    spool_path = str(cfg.get("spool_path") or os.path.expanduser("~/.local/state/mindmemos/claude-code-spool.sqlite3"))
    state_path = str(
        cfg.get("state_path") or os.path.expanduser("~/.local/state/mindmemos/claude-code-prompts.sqlite3")
    )
    client = DurableIngestClient(service_url, key, spool_path, timeout=0.75)
    prompts = PromptStore(state_path)
    hook = str(event.get("hook_event_name") or event.get("event_name") or "")
    session_id = str(event.get("session_id") or "unknown-session")
    transcript_path = event.get("transcript_path")

    if hook == "UserPromptSubmit":
        prompt = _text(event.get("prompt") or event.get("user_prompt"))
        if prompt:
            prompts.remember_prompt(
                session_id,
                _prompt_id(event, prompt, transcript_path),
                prompt,
                str(transcript_path) if transcript_path else None,
            )
        client.flush(limit=5)
        return None

    if hook != "Stop":
        client.flush(limit=5)
        return None

    row = prompts.latest(session_id)
    transcript_user, transcript_assistant = _latest_pair(transcript_path)
    if row is not None:
        user_message = str(row["prompt"])
        prompt_id = str(row["prompt_id"])
    elif transcript_user is not None:
        user_message = str(transcript_user["content"])
        prompt_id = str(transcript_user["id"])
    else:
        client.flush(limit=5)
        return None

    assistant_message, lagged_assistant = _assistant_with_lag(event, transcript_path)
    if not assistant_message and transcript_assistant is not None:
        assistant_message = str(transcript_assistant["content"])
    if not assistant_message:
        client.flush(limit=5)
        return None

    turn_hint = (
        event.get("turn_id")
        or event.get("stop_hook_id")
        or (lagged_assistant or transcript_assistant or {}).get("id")
        or stable_event_id("assistant", assistant_message)
    )
    event_id = stable_event_id("claude", session_id, prompt_id, turn_hint, assistant_message)
    payload = {
        "event_id": event_id,
        "session_id": session_id,
        "turn_id": str(turn_hint),
        "user_message": user_message,
        "assistant_message": assistant_message,
        "started_at": event.get("prompt_timestamp"),
        "completed_at": event.get("timestamp") or event.get("completed_at"),
        "safe_context": {"runtime": "claude_code", "hook": "Stop"},
    }
    client.enqueue("/ingest/turn", {key: value for key, value in payload.items() if value is not None})
    if row is not None:
        prompts.complete(int(row["id"]), event_id, assistant_message)
    client.flush(limit=10)
    return event_id


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        handle_event(event)
    except Exception as exc:
        print(f"[mindmemos-hook] {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
