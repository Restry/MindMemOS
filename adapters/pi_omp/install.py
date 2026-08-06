#!/usr/bin/env python3
"""Install the Pi/OMP extension without changing live daemon or settings files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def default_agent_root() -> Path:
    """Prefer this machine's active OMP profile, then fall back to legacy Pi."""

    override = os.getenv("MINDMEMOS_PI_OMP_AGENT_ROOT")
    if override:
        return Path(override).expanduser()
    omp_root = Path.home() / ".omp/agent"
    return omp_root if omp_root.exists() else Path.home() / ".pi/agent"


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
    target.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "mindmemos-provenance.ts", target)
    target.chmod(0o644)

    if key:
        key_file.write_text(key.strip() + "\n", encoding="utf-8")
        key_file.chmod(0o600)

    config = {
        "service_url": service_url.rstrip("/"),
        "key_file": str(key_file),
        "spool_dir": str(Path.home() / ".local/state/mindmemos/pi-omp-spool"),
        "timeout_ms": 750,
        "primary_only": True,
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    return {
        "extension": str(target),
        "config": str(config_path),
        "activation": "load the extension in the primary Pi/OMP process",
        "warning": "processes started with --no-extensions will not load it",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install MindMemOS Pi/OMP agent_end extension; settings remain untouched"
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=default_agent_root() / "extensions/mindmemos-provenance.ts",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.config/mindmemos/pi-omp.json").expanduser(),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path("~/.config/mindmemos/pi-omp.key").expanduser(),
    )
    parser.add_argument("--service-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    result = install(
        args.target,
        args.config,
        service_url=args.service_url,
        key_file=args.key_file,
        key=os.getenv("MINDMEMOS_INGEST_KEY", ""),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
