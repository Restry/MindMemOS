"""Optional FastAPI adapter for MindMemOS Lite.

Importing :mod:`mindmemos.runtime` never imports this package.  HTTP hosting is
therefore an adapter over the same runtime, not a second application lifecycle.
"""

from typing import Any


def create_app(**kwargs: Any):
    """Import FastAPI lazily so the base runtime has no HTTP dependency."""

    from .app import create_app as create_fastapi_app

    return create_fastapi_app(**kwargs)

__all__ = ["create_app"]
