"""MindMemOS external memory provider for Hermes Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
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
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|[\r\n]+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.:/+-]*", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_MARKDOWN_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)、]\s*)")


def _query_terms(text: str) -> set[str]:
    lowered = (text or "").lower()
    terms = {word for word in _WORD_RE.findall(lowered) if len(word) >= 2}
    for run in _CJK_RE.findall(lowered):
        if len(run) <= 2:
            terms.add(run)
            continue
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _extractive_excerpt(query: str, content: str, max_chars: int) -> str:
    """Return query-focused source sentences without generating new claims."""
    normalized = re.sub(r"```(?:[a-zA-Z0-9_+-]+)?", " ", content or "")
    sentences: list[tuple[int, str]] = []
    for index, raw in enumerate(_SENTENCE_SPLIT_RE.split(normalized)):
        sentence = _MARKDOWN_PREFIX_RE.sub("", re.sub(r"\s+", " ", raw)).strip()
        if sentence:
            sentences.append((index, sentence))
    if not sentences:
        return ""

    query_terms = _query_terms(query)
    ranked: list[tuple[int, int, str]] = []
    for index, sentence in sentences:
        overlap = query_terms.intersection(_query_terms(sentence))
        score = sum(max(1, len(term)) for term in overlap)
        ranked.append((score, index, sentence))
    positive = [item for item in ranked if item[0] > 0]
    chosen = sorted(
        sorted(positive or ranked, key=lambda item: (-item[0], item[1]))[:2],
        key=lambda item: item[1],
    )
    excerpt = " ".join(item[2] for item in chosen).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max(1, max_chars - 1)].rstrip() + "…"
    return excerpt


def _is_low_information_input(text: str) -> bool:
    cleaned = (text or "").strip()
    return cleaned.startswith("/") or bool(_LOW_INFORMATION_RE.fullmatch(cleaned))


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
        self._topic = ""
        self._recall_limit = 6
        self._timeout = 20.0
        self._ingest_url = ""
        self._auto_ingest = True
        self._background_flush = True
        self._auto_context_max_items = 3
        self._auto_context_chars = 1800
        self._auto_memory_chars = 560
        self._session_context_chars = 6000
        self._query_cache_seconds = 1800.0

        self._enabled = False
        self._session_id = ""
        self._agent_context = "primary"
        self._whoami = ""
        self._hermes_home = ""
        self._spool_dir = ""
        self._platform = ""
        self._profile = ""
        self._flush_lock = threading.Lock()
        self._capsule_lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._capsule_dir = ""
        self._audit_path = ""
        self._capsule_sessions: Dict[str, Dict[str, Any]] = {}
        self._apply_config(self._config)

    def _apply_config(self, config: Dict[str, Any]) -> None:
        self._config = dict(config)
        self._cfg = self._config
        self._api_key = str(
            self._config.get("api_key")
            or self._environ.get("MEM0_MCP_TOKEN", "")
            or self._environ.get("MINDMEMOS_API_KEY", "")
        ).strip()
        self._mcp_url = str(self._config.get("mcp_url", "")).rstrip("/")
        self._topic = str(self._config.get("topic", "")).strip()
        self._recall_limit = min(8, max(1, int(self._config.get("recall_limit", 8))))
        self._timeout = float(self._config.get("request_timeout_seconds", 20))
        self._ingest_url = str(self._config.get("ingest_url", "")).rstrip("/")
        self._auto_ingest = self._config.get("auto_ingest", True) is not False
        self._background_flush = self._config.get("background_flush", True) is not False
        self._auto_context_max_items = min(3, max(1, int(self._config.get("auto_context_max_items", 3))))
        self._auto_context_chars = min(2000, max(1000, int(self._config.get("auto_context_chars", 1800))))
        self._auto_memory_chars = min(700, max(240, int(self._config.get("auto_memory_chars", 560))))
        self._session_context_chars = min(
            20000, max(self._auto_context_chars, int(self._config.get("session_context_chars", 6000)))
        )
        self._query_cache_seconds = max(0.0, float(self._config.get("query_cache_seconds", 1800)))
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
                "MCP-Protocol-Version": "2025-11-25",
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
        if envelope.get("error"):
            raise RuntimeError(f"MindMemOS MCP request failed: {name}")
        result = envelope.get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"MindMemOS tool failed: {name}")
        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False)
        return "\n".join(
            item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"
        ).strip()

    def _resolve_authorized_topic(self) -> None:
        """Resolve one authorized topic without guessing across multiple topics."""
        payload = json.loads(self._call_mcp("list_topics", {}) or "{}")
        topics = payload.get("topics") if isinstance(payload, dict) else None
        if not isinstance(topics, list):
            self._topic = ""
            return

        configured = self._topic
        if configured:
            for topic in topics:
                if not isinstance(topic, dict):
                    continue
                topic_id = str(topic.get("id") or topic.get("topic_id") or "").strip()
                topic_name = str(topic.get("name") or topic.get("topic_name") or "").strip()
                if configured in {topic_id, topic_name}:
                    self._topic = topic_id or topic_name
                    return
            self._topic = ""
            return

        authorized = [topic for topic in topics if isinstance(topic, dict)]
        if len(authorized) == 1:
            only = authorized[0]
            self._topic = str(
                only.get("id") or only.get("topic_id") or only.get("name") or only.get("topic_name") or ""
            ).strip()
        else:
            self._topic = ""

    def _capsule_file(self, session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return os.path.join(self._capsule_dir, f"{digest}.json")

    def _load_capsule_session(self, session_id: str) -> Dict[str, Any]:
        state = self._capsule_sessions.get(session_id)
        if state is not None:
            return state
        state = {"seen": set(), "queries": {}, "chars": 0, "updated_at": time.time()}
        if self._capsule_dir:
            try:
                with open(self._capsule_file(session_id), "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                if raw.get("session_id") == session_id:
                    state["seen"] = {str(value) for value in raw.get("seen", []) if isinstance(value, str)}
                    raw_queries = raw.get("queries") or {}
                    if isinstance(raw_queries, dict):
                        state["queries"] = {
                            str(key): {
                                "fingerprints": {
                                    str(value) for value in (entry.get("fingerprints") or []) if isinstance(value, str)
                                },
                                "updated_at": float(entry.get("updated_at", 0)),
                            }
                            for key, entry in raw_queries.items()
                            if isinstance(entry, dict)
                        }
                    state["chars"] = max(0, int(raw.get("chars", 0)))
                    state["updated_at"] = float(raw.get("updated_at", time.time()))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug("MindMemOS capsule state ignored for %s: %s", session_id, exc)
        self._capsule_sessions[session_id] = state
        return state

    def _save_capsule_session(self, session_id: str, state: Dict[str, Any]) -> None:
        if not self._capsule_dir:
            return
        destination = self._capsule_file(session_id)
        temporary = destination + f".{uuid.uuid4().hex}.tmp"
        query_items = sorted(
            (state.get("queries") or {}).items(),
            key=lambda item: float(item[1].get("updated_at", 0)),
            reverse=True,
        )[:256]
        state["queries"] = dict(query_items)
        payload = {
            "version": 1,
            "session_id": session_id,
            "seen": sorted(state.get("seen") or []),
            "queries": {
                key: {
                    "fingerprints": sorted(entry.get("fingerprints") or []),
                    "updated_at": float(entry.get("updated_at", 0)),
                }
                for key, entry in query_items
            },
            "chars": int(state.get("chars") or 0),
            "updated_at": float(state.get("updated_at") or time.time()),
        }
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _memory_fingerprint(memory: Dict[str, Any]) -> str:
        content = str(memory.get("memory") or "").strip()
        memory_id = str(memory.get("id") or "").strip()
        version = str(memory.get("updated_at") or memory.get("last_update_at") or "").strip()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return hashlib.sha256(f"{memory_id}\0{version}\0{content_hash}".encode("utf-8")).hexdigest()

    @staticmethod
    def _query_fingerprint(query: str) -> str:
        normalized = re.sub(r"\s+", " ", (query or "").strip().lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _query_already_processed(self, session_id: str, query: str) -> bool:
        with self._capsule_lock:
            state = self._load_capsule_session(session_id)
            entry = state.get("queries", {}).get(self._query_fingerprint(query))
            return bool(
                entry
                and self._query_cache_seconds > 0
                and time.time() - float(entry.get("updated_at", 0)) < self._query_cache_seconds
            )

    def _session_has_capsule_budget(self, session_id: str) -> bool:
        with self._capsule_lock:
            state = self._load_capsule_session(session_id)
            return int(state.get("chars", 0)) < self._session_context_chars

    def _build_capsule(
        self,
        query: str,
        memories: List[Dict[str, Any]],
        session_id: str,
        audit: Optional[Dict[str, Any]] = None,
    ) -> str:
        heading = "## MindMemOS 记忆胶囊\n"
        note = "\n（仅含与当前问题相关的原文摘录；需要完整证据时请手动 recall。）"
        with self._capsule_lock:
            state = self._load_capsule_session(session_id)
            query_fingerprint = self._query_fingerprint(query)
            query_entry = state.get("queries", {}).get(query_fingerprint) or {}
            if (
                self._query_cache_seconds > 0
                and time.time() - float(query_entry.get("updated_at", 0)) < self._query_cache_seconds
            ):
                return ""
            query_seen = set(query_entry.get("fingerprints") or [])
            seen = state["seen"]
            session_remaining = self._session_context_chars - int(state["chars"])
            if session_remaining <= 0:
                return ""

            lines: list[str] = []
            fingerprints: list[str] = []
            candidate_fingerprints: set[str] = set()
            injected_ids: list[str] = []
            filtered: list[Dict[str, str]] = []
            if audit is not None:
                audit["candidate_ids"] = [str(memory.get("id") or "unknown") for memory in memories]
            used = len(heading) + len(note)
            for memory in memories:
                content = str(memory.get("memory") or "").strip()
                memory_id_full = str(memory.get("id") or "unknown")
                if not content:
                    filtered.append({"id": memory_id_full, "reason": "empty"})
                    continue
                fingerprint = self._memory_fingerprint(memory)
                candidate_fingerprints.add(fingerprint)
                if fingerprint in seen or fingerprint in query_seen:
                    filtered.append({"id": memory_id_full, "reason": "session_duplicate"})
                    continue
                if len(lines) >= self._auto_context_max_items:
                    filtered.append({"id": memory_id_full, "reason": "item_budget"})
                    continue
                excerpt = _extractive_excerpt(query, content, self._auto_memory_chars)
                if not excerpt:
                    filtered.append({"id": memory_id_full, "reason": "no_query_excerpt"})
                    continue
                memory_id = memory_id_full[:12]
                memory_type = str(memory.get("memory_type") or "fact")
                line = f"- [{memory_type}] {excerpt}（来源 {memory_id}）"
                projected = used + len(line) + 1
                if projected > self._auto_context_chars:
                    filtered.append({"id": memory_id_full, "reason": "turn_char_budget"})
                    continue
                if len(line) + 1 > session_remaining:
                    filtered.append({"id": memory_id_full, "reason": "session_char_budget"})
                    continue
                lines.append(line)
                fingerprints.append(fingerprint)
                injected_ids.append(memory_id_full)
                used = projected
                session_remaining -= len(line) + 1

            state.setdefault("queries", {})[query_fingerprint] = {
                "fingerprints": candidate_fingerprints,
                "updated_at": time.time(),
            }
            if audit is not None:
                audit.update({"filtered": filtered, "injected_ids": injected_ids})
            if not lines:
                state["updated_at"] = time.time()
                try:
                    self._save_capsule_session(session_id, state)
                except Exception as exc:
                    logger.warning("MindMemOS capsule state could not be persisted: %s", exc)
                return ""
            seen.update(fingerprints)
            injected_chars = sum(len(line) + 1 for line in lines)
            state["chars"] = int(state["chars"]) + injected_chars
            state["updated_at"] = time.time()
            try:
                self._save_capsule_session(session_id, state)
            except Exception as exc:
                logger.warning("MindMemOS capsule state could not be persisted: %s", exc)
            return heading + "\n".join(lines) + note

    def _record_recall_audit(self, payload: Dict[str, Any]) -> None:
        if not self._audit_path:
            return
        record = {"timestamp": time.time(), **payload}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._audit_lock:
                fd = os.open(self._audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
                os.chmod(self._audit_path, 0o600)
        except Exception as exc:
            logger.warning("MindMemOS recall audit could not be persisted: %s", exc)

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
        self._capsule_dir = os.path.join(self._hermes_home, "mindmemos-capsules")
        os.makedirs(self._capsule_dir, mode=0o700, exist_ok=True)
        os.chmod(self._capsule_dir, 0o700)
        self._audit_path = os.path.join(self._hermes_home, "mindmemos-recall-audit.jsonl")
        with self._capsule_lock:
            self._load_capsule_session(self._session_id)
        if self._agent_context == "primary":
            try:
                self._resolve_authorized_topic()
            except Exception as exc:
                self._topic = ""
                logger.warning("MindMemOS list_topics failed; refusing to guess a topic: %s", exc)
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
        if self._agent_context != "primary" or not self._topic or not text or _is_low_information_input(text):
            return ""
        started = time.perf_counter()
        sid = session_id or self._session_id or "unknown"
        try:
            if not self._session_has_capsule_budget(sid) or self._query_already_processed(sid, text):
                return ""
            recalled = self._call_mcp(
                "recall",
                {
                    "query": text,
                    "limit": self._recall_limit,
                    "topic": self._topic,
                },
            )
            payload = json.loads(recalled)
            memories = (
                payload.get("results") or payload.get("memories")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(memories, list):
                return ""
            provider_trace: Dict[str, Any] = {}
            capsule = self._build_capsule(text, memories, sid, provider_trace)
            self._record_recall_audit(
                {
                    "session_id": sid,
                    "query_fingerprint": self._query_fingerprint(text),
                    "core": payload.get("quality_trace") or {},
                    "provider": provider_trace,
                    "injected_chars": len(capsule),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            return capsule
        except Exception as exc:
            self._record_recall_audit(
                {
                    "session_id": sid,
                    "query_fingerprint": self._query_fingerprint(text),
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            logger.warning("MindMemOS automatic recall failed; continuing without context: %s", exc)
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
        if not content or not self._topic:
            return False
        arguments = {
            "memory": content,
            "topic": self._topic,
            "source": "hermes-agent",
        }
        session_id = str(record.get("session_id") or "").strip()
        if session_id:
            arguments["source_thread_id"] = session_id
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
            or not self._topic
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
        # Tools are exposed through the separately configured native MCP server.
        return []

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "MindMemOS instance credential",
                "secret": True,
                "required": True,
                "env_var": "MEM0_MCP_TOKEN",
                "url": "http://192.168.1.246:18765/llms.txt",
            },
            {
                "key": "mcp_url",
                "description": "MindMemOS Streamable HTTP MCP endpoint",
                "required": True,
                "default": "http://192.168.1.246:18765/mcp",
            },
            {
                "key": "topic",
                "description": "Explicit authorized topic id or name; one visible topic is selected automatically",
                "required": False,
                "default": "",
            },

            {
                "key": "recall_limit",
                "description": "Maximum candidate memories fetched before capsule selection",
                "default": "8",
            },
            {
                "key": "auto_context_max_items",
                "description": "Maximum unique memories in one automatic recall capsule",
                "default": "3",
            },
            {
                "key": "auto_context_chars",
                "description": "Maximum characters in one automatic recall capsule",
                "default": "1800",
            },
            {
                "key": "session_context_chars",
                "description": "Maximum unique recall excerpt characters accumulated per session",
                "default": "6000",
            },
            {
                "key": "query_cache_seconds",
                "description": "Skip identical automatic recall queries within this session window",
                "default": "1800",
            },
            {
                "key": "auto_ingest",
                "description": "Legacy completed-turn capture (disabled for mem0-memory-service)",
                "default": "false",
                "choices": ["true", "false"],
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
        old_session_id = self._session_id
        target = new_session_id or old_session_id
        with self._capsule_lock:
            if reset:
                state = {
                    "seen": set(),
                    "queries": {},
                    "chars": 0,
                    "updated_at": time.time(),
                }
                self._capsule_sessions[target] = state
                try:
                    self._save_capsule_session(target, state)
                except Exception as exc:
                    logger.debug("MindMemOS new capsule state was not persisted: %s", exc)
            elif target not in self._capsule_sessions:
                if self._capsule_dir and os.path.exists(self._capsule_file(target)):
                    self._load_capsule_session(target)
                else:
                    parent = parent_session_id or old_session_id
                    parent_state = self._load_capsule_session(parent) if parent else None
                    if parent_state:
                        state = {
                            "seen": set(parent_state.get("seen") or []),
                            "queries": {
                                key: {
                                    "fingerprints": set(entry.get("fingerprints") or []),
                                    "updated_at": float(entry.get("updated_at", 0)),
                                }
                                for key, entry in (parent_state.get("queries") or {}).items()
                            },
                            "chars": int(parent_state.get("chars") or 0),
                            "updated_at": time.time(),
                        }
                        self._capsule_sessions[target] = state
                        try:
                            self._save_capsule_session(target, state)
                        except Exception as exc:
                            logger.debug("MindMemOS continued capsule state was not persisted: %s", exc)
                    else:
                        self._load_capsule_session(target)
        self._session_id = target

    def shutdown(self) -> None:
        self._flush_spool_once()
        with self._capsule_lock:
            for session_id, state in self._capsule_sessions.items():
                try:
                    self._save_capsule_session(session_id, state)
                except Exception as exc:
                    logger.debug("MindMemOS capsule state flush failed: %s", exc)


def register(ctx: Any) -> None:
    ctx.register_memory_provider(MindMemOSProvider())
