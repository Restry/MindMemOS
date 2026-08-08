"""MindMemOS external memory provider for Hermes Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import urllib.request
import uuid
from typing import Any, Dict, List, Mapping, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_LOW_INFORMATION_RE = re.compile(
    r"^(?:(?:好的?|好|谢谢(?:你)?|多谢|收到|明白(?:了)?|知道了|了解|继续|可以|行|没问题|"
    r"ok(?:ay)?|yes|嗯+|哦+|对(?:的)?|是的)[\s，,。.!！?？]*)+$",
    re.IGNORECASE,
)
_COMMAND_PREFIXES = ("/", "!")
_SPLIT_RE = re.compile(r"(?:^|[\s，,；;。])\s*(?:\d+[)）.、]|[一二三四五六七八九十]+[)）.、])\s*")


def _is_low_information_input(text: str) -> bool:
    return bool(_LOW_INFORMATION_RE.fullmatch((text or "").strip()))


def _load_profile_config(environ: Mapping[str, str]) -> Dict[str, Any]:
    home = str(environ.get("HERMES_HOME", "")).strip()
    if not home:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    path = os.path.join(os.path.expanduser(home), "mindmemos.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not load MindMemOS config %s: %s", path, exc)
        return {}


class MindMemOSProvider(MemoryProvider):
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._explicit_config = config is not None
        self._config = dict(config) if config is not None else _load_profile_config(self._environ)
        self._cfg = self._config
        self._api_key = ""
        self._mcp_url = ""
        self._recall_limit = 6
        self._timeout = 20.0
        self._ingest_url = ""
        self._auto_ingest = True
        self._background_flush = True

        self._enabled = False
        self._session_id = ""
        self._agent_context = "primary"
        self._whoami = ""
        self._hermes_home = ""
        self._spool_dir = ""
        self._platform = ""
        self._profile = ""
        self._flush_lock = threading.Lock()
        self._apply_config(self._config)

    def _apply_config(self, config: Dict[str, Any]) -> None:
        self._config = dict(config)
        self._cfg = self._config
        self._api_key = str(self._config.get("api_key") or self._environ.get("MINDMEMOS_API_KEY", "")).strip()
        self._mcp_url = str(self._config.get("mcp_url", "")).rstrip("/")
        self._recall_limit = min(8, max(1, int(self._config.get("recall_limit", 8))))
        self._timeout = float(self._config.get("request_timeout_seconds", 20))
        self._ingest_url = str(self._config.get("ingest_url", "")).rstrip("/")
        self._auto_ingest = self._config.get("auto_ingest", True) is not False
        self._background_flush = self._config.get("background_flush", True) is not False
        self._enabled = bool(self._api_key and self._mcp_url)

    @property
    def name(self) -> str:
        return "mindmemos"

    def is_available(self) -> bool:
        return self._enabled

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.load(response)

    def _call_mcp(self, name: str, arguments: Dict[str, Any]) -> str:
        envelope = self._post_json(
            self._mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = envelope.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"MindMemOS tool failed: {name}")
        return "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        ).strip()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._hermes_home = str(
            kwargs.get("hermes_home") or self._environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        )
        if not self._explicit_config:
            config_environ = dict(self._environ)
            config_environ["HERMES_HOME"] = self._hermes_home
            self._apply_config(_load_profile_config(config_environ))
        self._platform = str(kwargs.get("platform") or "")
        self._profile = str(kwargs.get("agent_identity") or "default")

        if not self._enabled:
            return

        self._spool_dir = os.path.join(self._hermes_home, "mindmemos-spool")
        os.makedirs(self._spool_dir, mode=0o700, exist_ok=True)
        os.chmod(self._spool_dir, 0o700)
        if self._agent_context == "primary":
            try:
                self._whoami = self._call_mcp("whoami", {})
            except Exception as exc:
                logger.warning("MindMemOS whoami failed; continuing without it: %s", exc)
            if self._ingest_url:
                self._start_flush()

    def system_prompt_block(self) -> str:
        if self._agent_context != "primary":
            return ""
        identity = f"\n\n## 用户画像与高优先级规则\n{self._whoami}" if self._whoami else ""
        return (
            "# MindMemOS 长期记忆\n"
            "已启用跨机器长期记忆。相关记忆会在每轮自动召回；深度补查、项目约束和显式保存请使用 mcp_mindmemos_* 工具。"
            + identity
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        text = (query or "").strip()
        if self._agent_context != "primary" or not text or _is_low_information_input(text):
            return ""
        try:
            recalled = self._call_mcp("recall", {"query": text, "limit": self._recall_limit})
            return f"## MindMemOS 相关长期记忆\n{recalled}" if recalled else ""
        except Exception as exc:
            logger.debug("MindMemOS recall failed: %s", exc)
            return ""

    def _write_spool_record(self, record: Dict[str, Any], event_id: str) -> str:
        if not self._spool_dir:
            raise RuntimeError("MindMemOS provider is not initialized")
        destination = os.path.join(self._spool_dir, f"{event_id}.json")
        temporary = os.path.join(self._spool_dir, f".{event_id}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination

    def _send_turn(self, payload: Dict[str, Any]) -> bool:
        request = urllib.request.Request(
            self._ingest_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            status = getattr(response, "status", response.getcode())
            body = json.load(response)
        return status == 202 and body.get("ok") is True

    def _send_remember(self, record: Dict[str, Any]) -> bool:
        content = str(record.get("content") or "").strip()
        if not content:
            return False
        arguments = {"content": content}
        session_id = str(record.get("session_id") or "").strip()
        if session_id:
            arguments["session_id"] = session_id
        return bool(self._call_mcp("remember", arguments))

    def _flush_spool_once(self) -> None:
        if not self._spool_dir or not os.path.isdir(self._spool_dir):
            return
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            for name in sorted(os.listdir(self._spool_dir)):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                path = os.path.join(self._spool_dir, name)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        record = json.load(handle)
                    acknowledged = False
                    if record.get("kind") == "turn":
                        acknowledged = self._send_turn(record.get("payload") or {})
                    elif record.get("kind") == "remember":
                        acknowledged = self._send_remember(record)
                    if acknowledged:
                        os.unlink(path)
                except Exception as exc:
                    logger.debug("MindMemOS spool delivery retained %s: %s", path, exc)
        finally:
            self._flush_lock.release()

    def _start_flush(self) -> None:
        if self._background_flush:
            threading.Thread(target=self._flush_spool_once, daemon=True).start()

    def _worth_writing(self, user_content: str) -> bool:
        text = (user_content or "").strip()
        minimum = int(self._cfg.get("min_write_chars", 24))
        return len(text) >= minimum and not text.startswith(_COMMAND_PREFIXES) and not _is_low_information_input(text)

    @staticmethod
    def _is_recursive_capture(messages: Optional[List[Dict[str, Any]]]) -> bool:
        for message in messages or []:
            metadata = message.get("metadata") if isinstance(message, dict) else None
            provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
            if isinstance(provenance, dict) and provenance.get("capture_mode"):
                return True
        return False

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:

        if (
            self._agent_context != "primary"
            or not self._auto_ingest
            or not self._ingest_url
            or not self._worth_writing(user_content)
            or not (assistant_content or "").strip()
            or self._is_recursive_capture(messages)
        ):
            return
        sid = session_id or self._session_id or "unknown"
        digest = hashlib.sha256(
            (sid + "\0" + user_content + "\0" + (assistant_content or "")).encode("utf-8")
        ).hexdigest()
        event_id = f"hermes-{digest[:32]}"
        payload = {
            "event_id": event_id,
            "session_id": sid,
            "turn_id": digest[32:48],
            "user_message": user_content,
            "assistant_message": assistant_content or "",
            "safe_context": {
                "runtime": "hermes-agent",
                "platform": self._platform,
                "profile": self._profile,
                "capture_mode": "completed_turn",
            },
        }
        self._write_spool_record({"kind": "turn", "payload": payload}, event_id)
        self._start_flush()

    @staticmethod
    def _has_mindmemos_provenance(content: str, metadata: Optional[Dict[str, Any]]) -> bool:
        provenance = json.dumps(metadata or {}, ensure_ascii=False).lower()
        text = (content or "").lower()
        return "mindmemos" in provenance or text.startswith("mindmemos recall")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:

        if (
            self._agent_context != "primary"
            or action not in {"add", "replace"}
            or not (content or "").strip()
            or self._has_mindmemos_provenance(content, metadata)
        ):
            return
        sid = str((metadata or {}).get("session_id") or self._session_id or "unknown")
        digest = hashlib.sha256(
            ("remember\0" + sid + "\0" + target + "\0" + action + "\0" + content).encode("utf-8")
        ).hexdigest()
        event_id = f"hermes-remember-{digest[:24]}"
        record = {
            "kind": "remember",
            "content": content,
            "session_id": sid,
            "safe_context": {
                "runtime": "hermes-agent",
                "target": target,
                "action": action,
                "capture_mode": "explicit_memory_write",
            },
        }
        self._write_spool_record(record, event_id)
        self._start_flush()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # The same six tools are exposed through the configured MCP server.
        return []

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "MindMemOS instance credential",
                "secret": True,
                "required": True,
                "env_var": "MINDMEMOS_API_KEY",
                "url": "http://192.168.1.246:8666",
            },
            {
                "key": "mcp_url",
                "description": "MindMemOS Streamable HTTP MCP endpoint",
                "required": True,
                "default": "https://memory.studio.nexora.restry.cn/mcp",
            },
            {
                "key": "ingest_url",
                "description": "Durable completed-turn collector endpoint",
                "required": True,
                "default": "https://memory.studio.nexora.restry.cn/ingest/turn",
            },
            {
                "key": "recall_limit",
                "description": "Maximum memories injected before each turn",
                "default": "6",
            },
            {
                "key": "auto_ingest",
                "description": "Capture completed primary-agent turns",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "min_write_chars",
                "description": "Minimum non-acknowledgement user-message length for automatic capture",
                "default": "24",
            },
            {
                "key": "request_timeout_seconds",
                "description": "MindMemOS HTTP request timeout",
                "default": "20",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from pathlib import Path

        from utils import atomic_json_write

        path = Path(hermes_home) / "mindmemos.json"
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        safe_values = {k: v for k, v in values.items() if k not in {"api_key", "token"}}
        existing.update(safe_values)
        atomic_json_write(path, existing, mode=0o600)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        self._session_id = new_session_id or self._session_id

    def shutdown(self) -> None:
        self._flush_spool_once()


def register(ctx: Any) -> None:
    ctx.register_memory_provider(MindMemOSProvider())
