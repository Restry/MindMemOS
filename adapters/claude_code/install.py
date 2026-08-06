#!/usr/bin/env python3
"""Install the Claude Code adapter without editing live Claude settings."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON_CLIENT = HERE.parent / "python" / "mindmemos_ingest_client.py"


def settings_snippet(hook_path: Path) -> dict:
    command = f"python3 {hook_path}"
    hook = {"type": "command", "command": command, "timeout": 5}
    return {
        "hooks": {
            "UserPromptSubmit": [{"hooks": [hook]}],
            "Stop": [{"hooks": [hook]}],
        }
    }


def install(
    target: Path,
    config_path: Path,
    *,
    service_url: str,
    key_file: Path,
    key: str = "",
) -> dict:
    target = target.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    key_file = key_file.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)

    hook_target = target / "mindmemos_hook.py"
    client_target = target / "mindmemos_ingest_client.py"
    shutil.copy2(HERE / "mindmemos_hook.py", hook_target)
    shutil.copy2(COMMON_CLIENT, client_target)
    hook_target.chmod(0o755)
    client_target.chmod(0o755)

    if key:
        key_file.write_text(key.strip() + "\n", encoding="utf-8")
        key_file.chmod(0o600)

    config = {
        "service_url": service_url.rstrip("/"),
        "key_file": str(key_file),
        "spool_path": str(Path.home() / ".local/state/mindmemos/claude-code-spool.sqlite3"),
        "state_path": str(Path.home() / ".local/state/mindmemos/claude-code-prompts.sqlite3"),
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    return settings_snippet(hook_target)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install MindMemOS Claude hooks; does not modify ~/.claude/settings.json"
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("~/.local/share/mindmemos/claude-code").expanduser(),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.config/mindmemos/claude-code.json").expanduser(),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("~/.config/mindmemos/claude-code.key").expanduser(),
    )
    parser.add_argument("--service-url", default="http://127.0.0.1:8765")
    parser.add_argument("--snippet-only", action="store_true")
    args = parser.parse_args()

    hook_path = args.target.expanduser().resolve() / "mindmemos_hook.py"
    if args.snippet_only:
        snippet = settings_snippet(hook_path)
    else:
        snippet = install(
            args.target,
            args.config,
            service_url=args.service_url,
            key_file=args.key_file,
            key=os.getenv("MINDMEMOS_INGEST_KEY", ""),
        )
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
