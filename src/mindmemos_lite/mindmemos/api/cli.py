"""Console entry point for the optional MindMemOS Lite HTTP server."""

from __future__ import annotations

import argparse
import os

from .auth import API_KEY_FILE_ENV


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MindMemOS Lite with FastAPI")
    parser.add_argument("--host", default=os.getenv("MINDMEMOS_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MINDMEMOS_API_PORT", "8000")))
    parser.add_argument("--config", default=os.getenv("MINDMEMOS_CONFIG_PATH"))
    parser.add_argument("--api-key-file", default=os.getenv(API_KEY_FILE_ENV))
    args = parser.parse_args()

    if args.config:
        os.environ["MINDMEMOS_CONFIG_PATH"] = args.config
    if args.api_key_file:
        os.environ[API_KEY_FILE_ENV] = args.api_key_file

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised only in base-only installs
        raise SystemExit("FastAPI hosting is optional; install MindMemOS Lite with the 'api' extra") from exc

    uvicorn.run("mindmemos.api.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
