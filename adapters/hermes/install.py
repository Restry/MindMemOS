#!/usr/bin/env python3
"""Install the repository-owned MindMemOS Hermes memory provider.

This installer copies only plugin source/manifest. It never copies credentials,
overwrites mindmemos.json, or changes Hermes memory settings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "mindmemos"
FILES = ("__init__.py", "plugin.yaml")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(hermes_home: Path) -> dict:
    target = hermes_home / "plugins" / "mindmemos"
    files = {}
    for name in FILES:
        source = SOURCE / name
        installed = target / name
        files[name] = {
            "installed": installed.exists(),
            "matches_source": installed.exists() and digest(installed) == digest(source),
        }
    return {
        "ok": all(item["matches_source"] for item in files.values()),
        "source": str(SOURCE),
        "target": str(target),
        "files": files,
    }


def install(hermes_home: Path) -> dict:
    target = hermes_home / "plugins" / "mindmemos"
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = SOURCE / name
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=target)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return status(hermes_home)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hermes-home",
        default=os.getenv("HERMES_HOME", "~/.hermes"),
        help="Hermes profile home (default: HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument("--check", action="store_true", help="Check without changing files")
    args = parser.parse_args()
    home = Path(args.hermes_home).expanduser().resolve()
    result = status(home) if args.check else install(home)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
