from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_python_adapter_spool_retries_and_keeps_done_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_module = _module(
        "test_mindmemos_ingest_client",
        ROOT / "adapters/python/mindmemos_ingest_client.py",
    )
    client = client_module.DurableIngestClient(
        "http://collector.invalid",
        "secret-not-printed",
        str(tmp_path / "spool.sqlite3"),
        base_backoff_seconds=0,
    )
    assert oct((tmp_path / "spool.sqlite3").stat().st_mode & 0o777) == "0o600"
    payload = {
        "event_id": "adapter-event",
        "session_id": "session",
        "turn_id": "turn",
        "user_message": "fact",
        "assistant_message": "answer",
    }
    assert client.enqueue("/ingest/turn", payload)
    assert not client.enqueue("/ingest/turn", payload)

    monkeypatch.setattr(
        client_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    assert client.flush_once().status == "error"
    assert client.counts()["error"] == 1

    class Accepted:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"ok": true}'

    monkeypatch.setattr(client_module.urllib.request, "urlopen", lambda *_args, **_kwargs: Accepted())
    assert client.flush_once().status == "done"
    assert client.counts()["done"] == 1
    assert not client.enqueue("/ingest/turn", payload)


def test_claude_hook_pairs_prompt_stop_and_deduplicates_duplicate_stop(tmp_path: Path) -> None:
    hook = _module(
        "test_mindmemos_claude_hook",
        ROOT / "adapters/claude_code/mindmemos_hook.py",
    )
    config = {
        "service_url": "http://127.0.0.1:9",
        "key_file": str(tmp_path / "missing.key"),
        "spool_path": str(tmp_path / "spool.sqlite3"),
        "state_path": str(tmp_path / "prompts.sqlite3"),
    }
    prompt_event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "claude-session",
        "prompt_id": "prompt-1",
        "prompt": "Remember the stable decision",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    stop_event = {
        "hook_event_name": "Stop",
        "session_id": "claude-session",
        "stop_hook_id": "stop-1",
        "last_assistant_message": "The stable decision is complete.",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    hook.handle_event(prompt_event, config)
    first_id = hook.handle_event(stop_event, config)
    second_id = hook.handle_event(stop_event, config)
    assert first_id == second_id
    assert oct(Path(config["spool_path"]).stat().st_mode & 0o777) == "0o600"
    assert oct(Path(config["state_path"]).stat().st_mode & 0o777) == "0o600"

    with sqlite3.connect(config["spool_path"]) as connection:
        rows = connection.execute("SELECT event_id, payload_json, status FROM events").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload["user_message"] == prompt_event["prompt"]
    assert payload["assistant_message"] == stop_event["last_assistant_message"]
    assert "thinking" not in payload
    assert rows[0][2] == "pending"


def test_claude_hook_reads_lagged_transcript_without_tool_or_thinking_content(tmp_path: Path) -> None:
    hook = _module(
        "test_mindmemos_claude_hook_lag",
        ROOT / "adapters/claude_code/mindmemos_hook.py",
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": [{"type": "text", "text": "User prompt"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "private reasoning"},
                                {"type": "tool_use", "name": "read"},
                                {"type": "text", "text": "Final answer only"},
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    config = {
        "service_url": "http://127.0.0.1:9",
        "key_file": str(tmp_path / "missing.key"),
        "spool_path": str(tmp_path / "spool.sqlite3"),
        "state_path": str(tmp_path / "prompts.sqlite3"),
    }
    hook.handle_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session",
            "prompt_id": "user-1",
            "prompt": "User prompt",
            "transcript_path": str(transcript),
        },
        config,
    )
    hook.handle_event(
        {
            "hook_event_name": "Stop",
            "session_id": "session",
            "transcript_path": str(transcript),
        },
        config,
    )
    with sqlite3.connect(config["spool_path"]) as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM events").fetchone()[0])
    assert payload["assistant_message"] == "Final answer only"


def test_installers_copy_adapters_without_touching_settings_or_exposing_keys(tmp_path: Path) -> None:
    claude_installer = _module(
        "test_claude_installer",
        ROOT / "adapters/claude_code/install.py",
    )
    claude_target = tmp_path / "claude"
    claude_config = tmp_path / "claude.json"
    claude_key = tmp_path / "claude.key"
    secret = "test-secret-value"
    snippet = claude_installer.install(
        claude_target,
        claude_config,
        service_url="http://collector",
        key_file=claude_key,
        key=secret,
    )
    assert (claude_target / "mindmemos_hook.py").exists()
    assert (claude_target / "mindmemos_ingest_client.py").exists()
    assert oct(claude_key.stat().st_mode & 0o777) == "0o600"
    assert secret not in json.dumps(snippet)

    pi_installer = _module("test_pi_installer", ROOT / "adapters/pi_omp/install.py")
    pi_target = tmp_path / "pi" / "mindmemos-provenance.ts"
    pi_config = tmp_path / "pi.json"
    pi_key = tmp_path / "pi.key"
    result = pi_installer.install(
        pi_target,
        pi_config,
        service_url="http://collector",
        key_file=pi_key,
        key=secret,
    )
    assert pi_target.exists()
    assert oct(pi_key.stat().st_mode & 0o777) == "0o600"
    assert secret not in json.dumps(result)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_pi_omp_installer_uses_explicit_active_agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _module("test_pi_omp_default_root", ROOT / "adapters/pi_omp/install.py")
    active_root = tmp_path / "active-omp"
    monkeypatch.setenv("MINDMEMOS_PI_OMP_AGENT_ROOT", str(active_root))
    assert installer.default_agent_root() == active_root
