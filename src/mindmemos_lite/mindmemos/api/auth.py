"""Standalone API-key authentication for the optional HTTP adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from ..config import DEFAULT_MINDMEMOS_CONFIG_ROOT
from ..errors import AuthenticationError

API_KEY_FILE_ENV = "MINDMEMOS_API_KEY_FILE"
STANDALONE_ACCOUNT_ID = "memory_standalone"


@dataclass(frozen=True, slots=True)
class ResolvedApiKey:
    """Trusted project context resolved from one bearer credential."""

    account_id: str
    project_id: str
    key_id: str
    memory_algorithm: str
    scopes: tuple[str, ...]


class ApiKeyProvider(Protocol):
    """Resolve opaque bearer credentials without exposing storage details."""

    def resolve(self, api_key: str) -> ResolvedApiKey: ...


class FileApiKeyProvider:
    """Read standalone API keys from a YAML file at process startup."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._keys = self._load(self.path)

    @classmethod
    def from_env(cls) -> "FileApiKeyProvider":
        path = os.getenv(API_KEY_FILE_ENV)
        return cls(path or DEFAULT_MINDMEMOS_CONFIG_ROOT / "api_keys.yaml")

    def resolve(self, api_key: str) -> ResolvedApiKey:
        try:
            return self._keys[api_key]
        except KeyError as exc:
            raise AuthenticationError("invalid API key", code="auth.invalid_api_key") from exc

    @staticmethod
    def _load(path: Path) -> dict[str, ResolvedApiKey]:
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = yaml.safe_load(file) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(f"API key file not found: {path}; set {API_KEY_FILE_ENV} to a valid YAML file") from exc

        entries = payload.get("api_keys")
        if not isinstance(entries, list):
            raise RuntimeError(f"API key file {path} must contain an 'api_keys' list")

        resolved: dict[str, ResolvedApiKey] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RuntimeError(f"api_keys[{index}] in {path} must be a mapping")
            if not entry.get("enabled", True):
                continue
            api_key = _required_text(entry, "api_key", path, index)
            if api_key in resolved:
                raise RuntimeError(f"duplicate api_key at api_keys[{index}] in {path}")
            scopes = entry.get("scopes") or []
            if not isinstance(scopes, list) or not all(isinstance(scope, str) and scope for scope in scopes):
                raise RuntimeError(f"api_keys[{index}].scopes in {path} must be a list of strings")
            resolved[api_key] = ResolvedApiKey(
                account_id=str(entry.get("account_id") or STANDALONE_ACCOUNT_ID),
                project_id=_required_text(entry, "project_id", path, index),
                key_id=_required_text(entry, "key_id", path, index),
                memory_algorithm=str(entry.get("memory_algorithm") or "vanilla"),
                scopes=tuple(scopes),
            )
        return resolved


def _required_text(entry: dict, field: str, path: Path, index: int) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"api_keys[{index}].{field} in {path} must be a non-empty string")
    return value.strip()


__all__ = ["API_KEY_FILE_ENV", "ApiKeyProvider", "FileApiKeyProvider", "ResolvedApiKey"]
