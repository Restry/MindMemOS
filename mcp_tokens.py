#!/usr/bin/env python3
"""MindMemOS MCP token 存储 —— 面板和 MCP server 共用的唯一真源。

设计要点：
  - 每个客户端一条 token，可命名、可撤销、可设过期，互不牵连
  - scope 分 read / write，默认只读；只有显式给 write 才能调 remember
  - 明文只在「生成的那一刻」返回一次，之后库里只留 sha256，面板也看不到
  - 保留 legacy 单 token 文件（~/.hermes/mindmemos_mcp_token）做向后兼容，
    避免本机已接入的客户端一次性全断；legacy 视为 write scope

存储：~/.hermes/mindmemos_mcp_tokens.json  (0600)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

STORE = os.path.expanduser(os.getenv("MM_MCP_TOKEN_STORE", "~/.hermes/mindmemos_mcp_tokens.json"))
LEGACY = os.path.expanduser(os.getenv("MM_MCP_LEGACY_TOKEN", "~/.hermes/mindmemos_mcp_token"))

WRITE_TOOLS = {"remember"}
_LOCK = threading.RLock()
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,95}$")


@dataclass(frozen=True)
class Principal:
    """Server-resolved producer identity. Never populated from MCP tool arguments."""

    client_id: str
    agent_kind: str
    instance: str
    credential_id: str
    display_name: str
    scope: str
    authority: str = "credential"

    @property
    def app_id(self) -> str:
        return self.client_id

    @property
    def agent_id(self) -> str:
        return f"{self.agent_kind}:{self.instance}"

    def as_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["app_id"] = self.app_id
        data["agent_id"] = self.agent_id
        return data


@dataclass(frozen=True)
class AuthResult:
    principal: Principal | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.principal is not None


def _identifier(value: str | None, field: str) -> str:
    normalized = (value or "").strip().lower()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field} must match {_IDENTIFIER_RE.pattern}")
    return normalized


def _principal_for_record(record: dict[str, Any]) -> Principal:
    """Normalize new and pre-principal token records without rewriting the store."""

    credential_id = str(record.get("id") or "unknown")
    has_identity = all(record.get(key) for key in ("client_id", "agent_kind", "instance"))
    return Principal(
        client_id=str(record.get("client_id") or f"legacy-record-{credential_id}"),
        agent_kind=str(record.get("agent_kind") or "legacy"),
        instance=str(record.get("instance") or "unknown"),
        credential_id=credential_id,
        display_name=str(record.get("display_name") or record.get("name") or "Legacy token"),
        scope=str(record.get("scope") or "read"),
        authority=str(record.get("authority") or ("credential" if has_identity else "legacy_fallback")),
    )


def legacy_principal() -> Principal:
    return Principal(
        client_id="legacy-mcp-token",
        agent_kind="legacy",
        instance="shared",
        credential_id="legacy-file",
        display_name="Legacy MCP token",
        scope="write",
        authority="legacy_fallback",
    )


def local_fallback_principal(agent_kind: str = "stdio_mcp", instance: str | None = None) -> Principal:
    """Explicit identity for local stdio callers that have no :8765 credential."""

    resolved_kind = _identifier(agent_kind, "agent_kind")
    resolved_instance = _identifier(instance or socket.gethostname().split(".")[0], "instance")
    return Principal(
        client_id=f"local-{resolved_kind}-{resolved_instance}",
        agent_kind=resolved_kind,
        instance=resolved_instance,
        credential_id="local-config",
        display_name=resolved_instance,
        scope="write",
        authority="local_config",
    )


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _load() -> list[dict[str, Any]]:
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def issue(
    name: str,
    scope: str = "read",
    ttl_days: int | None = None,
    *,
    client_id: str | None = None,
    agent_kind: str | None = None,
    instance: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Issue a credential. Plaintext is returned once; identity survives rotation."""

    name = (name or "").strip() or "unnamed"
    scope = scope if scope in ("read", "write") else "read"
    if ttl_days is not None and ttl_days <= 0:
        raise ValueError("ttl_days must be positive")

    with _LOCK:
        items = _load()
        existing: Principal | None = None
        if client_id:
            normalized_client_id = _identifier(client_id, "client_id")
            existing = next(
                (
                    principal
                    for principal in map(_principal_for_record, items)
                    if principal.client_id == normalized_client_id
                ),
                None,
            )
        else:
            normalized_client_id = f"client-{secrets.token_hex(8)}"

        if existing is not None:
            if agent_kind and _identifier(agent_kind, "agent_kind") != existing.agent_kind:
                raise ValueError("agent_kind conflicts with the existing client_id")
            if instance and _identifier(instance, "instance") != existing.instance:
                raise ValueError("instance conflicts with the existing client_id")
            resolved_kind = existing.agent_kind
            resolved_instance = existing.instance
            resolved_display = (display_name or "").strip() or existing.display_name
            authority = existing.authority
        else:
            resolved_kind = _identifier(agent_kind or "unclassified", "agent_kind")
            resolved_instance = _identifier(instance or "unknown", "instance")
            resolved_display = (display_name or "").strip() or name
            authority = "credential" if agent_kind and instance else "fallback"

        tok = "mm_" + secrets.token_urlsafe(32)
        now = int(time.time())
        rec = {
            "id": secrets.token_hex(6),
            "name": name,
            "hash": _hash(tok),
            "scope": scope,
            "client_id": normalized_client_id,
            "agent_kind": resolved_kind,
            "instance": resolved_instance,
            "display_name": resolved_display,
            "authority": authority,
            "created": now,
            "expires": now + ttl_days * 86400 if ttl_days else None,
            "revoked": False,
            "last_used": None,
            "use_count": 0,
        }
        items.append(rec)
        _save(items)

    out = {k: v for k, v in rec.items() if k != "hash"}
    out["token"] = tok
    return out


def revoke(token_id: str) -> bool:
    with _LOCK:
        items = _load()
        hit = False
        for record in items:
            if record.get("id") == token_id and not record.get("revoked"):
                record["revoked"] = True
                record["revoked_at"] = int(time.time())
                hit = True
        if hit:
            _save(items)
        return hit


def listing() -> list[dict[str, Any]]:
    """Return credential metadata plus normalized principals, never hashes/plaintext."""

    with _LOCK:
        out = []
        for record in _load():
            visible = {k: v for k, v in record.items() if k not in {"hash", "token"}}
            visible.update(_principal_for_record(record).as_dict())
            out.append(visible)
        return out


def authenticate(token: str, required_scope: str = "read") -> AuthResult:
    """Resolve a trusted principal and enforce read/write scope."""

    token = (token or "").strip()
    if not token:
        return AuthResult(None, "empty")
    if required_scope not in ("read", "write"):
        raise ValueError("required_scope must be read or write")

    try:
        with open(LEGACY, encoding="utf-8") as f:
            legacy = f.read().strip()
        if legacy and secrets.compare_digest(token, legacy):
            return AuthResult(legacy_principal(), "legacy")
    except Exception:
        pass

    token_hash = _hash(token)
    now = int(time.time())
    with _LOCK:
        items = _load()
        for record in items:
            if not secrets.compare_digest(str(record.get("hash") or ""), token_hash):
                continue
            if record.get("revoked"):
                return AuthResult(None, "revoked")
            expires = record.get("expires")
            if expires and now > expires:
                return AuthResult(None, "expired")
            principal = _principal_for_record(record)
            if required_scope == "write" and principal.scope != "write":
                return AuthResult(None, "scope")
            record["last_used"] = now
            record["use_count"] = int(record.get("use_count") or 0) + 1
            try:
                _save(items)
            except Exception:
                pass
            return AuthResult(principal, "ok")
    return AuthResult(None, "unknown")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "issue":
        name = sys.argv[2] if len(sys.argv) > 2 else "cli"
        scope = sys.argv[3] if len(sys.argv) > 3 else "read"
        print(json.dumps(issue(name, scope), ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 3 and sys.argv[1] == "revoke":
        print("ok" if revoke(sys.argv[2]) else "not found")
    else:
        print(json.dumps(listing(), ensure_ascii=False, indent=2))
