"""MindMemOS memory provider — local long-term memory for Hermes.

Automatic recall: search before each primary turn and inject relevant history.
Automatic write: persist the completed primary turn to a local SQLite spool, then
submit it to the authenticated :8765 durable collector without blocking the reply.
Builtin-memory mirroring uses the same trusted collector and avoids recursive capture.

Configuration (``$HERMES_HOME/mindmemos.json``):
  base_url             MindMemOS recall API, default http://127.0.0.1:8000
  api_key              :8000 API key (or MINDMEMOS_API_KEY)
  ingest_url           durable collector base URL, default http://127.0.0.1:8765
  ingest_key           write Key dedicated to this Hermes + machine instance
  ingest_spool         local SQLite retry spool
  ingest_client_module dependency-free durable client module path
  user_id              memory owner, default leway
  top_k                recalled memories per turn, default 6
  score_threshold      recall threshold, default 0.1
  prefetch_rerank      rerank automatic recall, default true
  prefetch_timeout     per automatic recall request timeout, default 6s
  prefetch_parallelism max concurrent subqueries, default 1
  write_enabled        automatic primary-turn capture, default true
  min_write_chars      minimum user-message length, default 24
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_PREFETCH_TIMEOUT = 6.0  # 检索最多等这么久，超时就不注入（不能拖死对话）
_BREAKER_THRESHOLD = 4  # 连续失败几次后熔断
_BREAKER_COOLDOWN = 180.0  # 熔断冷却秒数

# Slash/bang commands are control input, not durable user content.
_COMMAND_PREFIXES = ("/", "!")

# Pure acknowledgements carry no retrieval intent.  Keep this exact rather than
# prefix-based so "继续检查 MindMemOS" still recalls project context.
_LOW_INFORMATION_RE = re.compile(
    r"^(?:(?:好的?|好|谢谢(?:你)?|多谢|收到|明白(?:了)?|知道了|了解|继续|可以|行|没问题|"
    r"ok(?:ay)?|yes|嗯+|哦+|对(?:的)?|是的)[\s，,。.!！?？]*)+$",
    re.IGNORECASE,
)


def _is_low_information_input(text: str) -> bool:
    return bool(_LOW_INFORMATION_RE.fullmatch((text or "").strip()))


_INGEST_MODULE = None


def _load_ingest_module(path: str):
    global _INGEST_MODULE
    if _INGEST_MODULE is not None:
        return _INGEST_MODULE
    spec = importlib.util.spec_from_file_location("hermes_mindmemos_ingest_client", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _INGEST_MODULE = module
    return module


class MindMemOSProvider(MemoryProvider):
    def __init__(self) -> None:
        self._cfg: Dict[str, Any] = {}
        self._base = ""
        self._key = ""
        self._user = "leway"
        self._session_id = ""
        self._platform = "cli"
        self._writable = True  # 非 primary context（cron/subagent）不写
        self._enabled = False

        self._prefetch_cache = ""
        self._last_result: tuple = ("", "", 0.0)  # (query, 结果文本, 时间戳)
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None

        self._ingest_module = None
        self._ingest_client = None

        self._fail_count = 0
        self._breaker_until = 0.0

    # ---------------------------------------------------------------- 基本信息

    @property
    def name(self) -> str:
        return "mindmemos"

    def _load_cfg(self, hermes_home: str) -> Dict[str, Any]:
        path = os.path.join(hermes_home, "mindmemos.json")
        cfg: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                cfg = json.load(open(path, encoding="utf-8"))
            except Exception as e:
                logger.warning("mindmemos.json 读取失败: %s", e)
        cfg.setdefault("base_url", os.getenv("MINDMEMOS_BASE_URL", "http://127.0.0.1:8000"))
        cfg.setdefault("api_key", os.getenv("MINDMEMOS_API_KEY", ""))
        cfg.setdefault("user_id", "leway")
        cfg.setdefault("top_k", 6)
        cfg.setdefault("score_threshold", 0.1)
        cfg.setdefault("prefetch_rerank", True)
        cfg.setdefault("prefetch_timeout", _PREFETCH_TIMEOUT)
        cfg.setdefault("prefetch_parallelism", 1)
        cfg.setdefault("write_enabled", True)
        cfg.setdefault("min_write_chars", 24)
        cfg.setdefault("ingest_url", os.getenv("MINDMEMOS_INGEST_URL", "http://127.0.0.1:8765"))
        cfg.setdefault("ingest_key", os.getenv("MINDMEMOS_INGEST_KEY", ""))
        cfg.setdefault(
            "ingest_spool",
            os.path.join(hermes_home, "mindmemos_hermes_ingest.sqlite3"),
        )
        cfg.setdefault(
            "ingest_client_module",
            os.path.expanduser("~/Projects/MindMemOS/adapters/python/mindmemos_ingest_client.py"),
        )
        return cfg

    def is_available(self) -> bool:
        home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
        cfg = self._load_cfg(home)
        # 只查配置，不做网络调用（ABC 要求）
        return bool(cfg.get("api_key"))

    # ---------------------------------------------------------------- 生命周期

    def initialize(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home") or os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
        self._cfg = self._load_cfg(home)
        self._base = str(self._cfg["base_url"]).rstrip("/")
        self._key = str(self._cfg["api_key"])
        self._user = str(self._cfg["user_id"])
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")

        ctx = kwargs.get("agent_context", "primary")
        self._writable = bool(self._cfg.get("write_enabled", True)) and ctx == "primary"
        self._enabled = bool(self._key)

        if not self._enabled:
            logger.warning("MindMemOS provider 未配置 api_key，已停用")
            return

        self._ingest_module = _load_ingest_module(os.path.expanduser(str(self._cfg["ingest_client_module"])))
        self._ingest_client = self._ingest_module.DurableIngestClient(
            str(self._cfg["ingest_url"]),
            str(self._cfg.get("ingest_key") or ""),
            str(self._cfg["ingest_spool"]),
        )
        self._ingest_client.start_worker()
        if self._writable and not self._cfg.get("ingest_key"):
            logger.warning("MindMemOS 自动写入 Key 未配置；轮次会留在本地 durable spool 等待配置")
        logger.info(
            "MindMemOS provider 就绪 base=%s ingest=%s writable=%s",
            self._base,
            self._cfg["ingest_url"],
            self._writable,
        )

    def shutdown(self) -> None:
        if self._ingest_client is not None:
            self._ingest_client.stop_worker(timeout=5.0)
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id
        with self._prefetch_lock:
            self._last_result = ("", "", 0.0)

    # ---------------------------------------------------------------- HTTP

    def _breaker_open(self) -> bool:
        return time.time() < self._breaker_until

    def _record_fail(self) -> None:
        self._fail_count += 1
        if self._fail_count >= _BREAKER_THRESHOLD:
            self._breaker_until = time.time() + _BREAKER_COOLDOWN
            self._fail_count = 0
            logger.warning("MindMemOS 连续失败，熔断 %ds", int(_BREAKER_COOLDOWN))

    def _post(self, path: str, payload: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
        if self._breaker_open():
            return None
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read())
            self._fail_count = 0
            return out
        except Exception as e:
            logger.debug("MindMemOS %s 失败: %s", path, str(e)[:200])
            self._record_fail()
            return None

    def _search(
        self,
        query: str,
        top_k: Optional[int] = None,
        timeout: float = _PREFETCH_TIMEOUT,
        rerank: bool = True,
    ) -> List[Dict[str, Any]]:
        d = self._post(
            "/v1/memory/search",
            {
                "user_id": self._user,
                "query": query,
                "top_k": int(top_k or self._cfg.get("top_k", 6)),
                "rerank": rerank,
                "score_threshold": float(self._cfg.get("score_threshold", 0.1)),
            },
            timeout,
        )
        if not d:
            return []
        return d.get("data", {}).get("memories", []) or []

    # ---------------------------------------------------------------- 自动检索

    @staticmethod
    def _format(mems: List[Dict[str, Any]]) -> str:
        if not mems:
            return ""
        lines = ["## 长期记忆（MindMemOS 自动召回）", ""]
        for m in mems:
            t = m.get("memory_type") or ""
            lines.append(f"- [{t}] {m.get('memory', '')}")
        lines.append("")
        lines.append("以上为过往会话沉淀的事实，可能已过时；与当前对话冲突时以当前对话为准。")
        return "\n".join(lines)

    def system_prompt_block(self) -> str:
        """常驻块：高权威身份/铁律不走语义检索，避免措辞差异导致召回不到。

        实测过 "怎么称呼用户" 这类问题 rerank 分数低于阈值被砍掉 —— 身份类
        记忆一旦漏召回，接管 memory 后就会忘掉最基本的规矩。所以这类内容
        常驻系统提示，语义检索只负责项目历史。
        """
        if not self._enabled:
            return ""
        parts = [
            "## MindMemOS 长期记忆已接管",
            "",
            "过往项目历史、决策、踩坑会在每轮自动召回并注入。需要补查特定主题时调用 `recall` 工具。",
            "召回为空表示库里确实没有，不要编造。",
        ]
        pinned = self._load_pinned()
        if pinned:
            parts += ["", "### 常驻铁律（高权威，优先级高于召回内容）", ""]
            parts += [f"- {p}" for p in pinned]
        return "\n".join(parts)

    def _load_pinned(self) -> List[str]:
        """读取常驻铁律。默认取内置 MEMORY.md / USER.md 的前若干条身份/偏好。"""
        path = self._cfg.get("pinned_file")
        if not path:
            home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
            path = os.path.join(home, "mindmemos_pinned.md")
        if not os.path.exists(path):
            return []
        try:
            raw = open(path, encoding="utf-8").read()
        except Exception:
            return []
        return [b.strip() for b in raw.split("\n§\n") if len(b.strip()) >= 8]

    _SPLIT_RE = re.compile(r"(?:^|[\s，,；;。])\s*(?:\d+[)）.、]|[一二三四五六七八九十]+[)）.、])\s*")

    # 身份类代词查询：这类问题靠字面语义检索必然召不回
    _IDENTITY_RE = re.compile(
        r"我是谁|我叫什么|我的名字|怎么称呼我|你认识我|我是什么人|"
        r"我的身份|了解我|关于我|我的家人|我家人|我的家庭"
    )

    def _subqueries(self, q: str) -> List[str]:
        """把「1)…2)…3)…」这种多问句拆成子问题。

        为什么要拆：rerank 是拿整句算相关度的。一句话里塞 5 个话题时
        每个话题的信号被稀释，实测「wingman 是什么项目」单问能召回 4 条，
        混在 5 问里一条都召不回。拆开分别检索再合并才不会漏。
        """
        if len(q) < 30:
            return [q]
        parts = [p.strip(" ：:，,。；;？?") for p in self._SPLIT_RE.split(q)]
        parts = [p for p in parts if len(p) >= 4]
        # 至少拆出 2 段才算多问句，否则按原样查
        return parts if len(parts) >= 2 else [q]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """按当轮真实问题检索并注入。带短期缓存，避免同问题重复烧算力。"""
        if not self._enabled:
            return ""
        q = (query or "").strip()
        if len(q) < 2:
            return ""
        if _is_low_information_input(q):
            return ""
        # 身份类代词查询（「我是谁」「我叫什么」）字面太短、语义又跟
        # 「用户称呼其父亲为爸爸」这类记忆原文距离很远，直查召不回。
        # 改写成陈述句式的关键词再检索。
        if self._IDENTITY_RE.search(q):
            q = "用户的称呼 姓名 别名 家庭成员 工作单位 时区 语言偏好"
        now = time.time()
        with self._prefetch_lock:
            key, text, ts = self._last_result
            if key == q and now - ts < 120:
                return text

        subs = self._subqueries(q)
        timeout = max(0.25, float(self._cfg.get("prefetch_timeout", _PREFETCH_TIMEOUT)))
        rerank_value = self._cfg.get("prefetch_rerank", True)
        rerank = str(rerank_value).strip().lower() not in {"0", "false", "no", "off"}
        parallelism = max(1, min(int(self._cfg.get("prefetch_parallelism", 1)), 3))
        if len(subs) == 1:
            out = self._format(self._search(q, timeout=timeout, rerank=rerank))
        else:
            merged, seen = [], set()
            if parallelism > 1:
                with ThreadPoolExecutor(max_workers=min(parallelism, len(subs))) as pool:
                    batches = list(
                        pool.map(
                            lambda sub: self._search(sub, timeout=timeout, rerank=rerank),
                            subs,
                        )
                    )
            else:
                batches = [self._search(s, timeout=timeout, rerank=rerank) for s in subs]
            for batch in batches:
                for m in batch:
                    txt = (m.get("memory") or "").strip()
                    if txt and txt not in seen:
                        seen.add(txt)
                        merged.append(m)
            out = self._format(merged)

        with self._prefetch_lock:
            self._last_result = (q, out, now)
        return out

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """本轮结束后的预取钩子 —— 故意不做任何事。

        ABC 的设计意图是「用本轮内容预热下一轮」，但下一轮爸爸问的是新问题，
        拿本轮的 query 去预取必然命中不了，缓存作废还白烧一次
        search + embedding + rerank（实测一次对话变成 2 次检索）。
        真正的检索在 prefetch() 里按当轮真实问题同步做，够快（~1s）。
        """
        return

    # ---------------------------------------------------------------- 自动写入

    def _worth_writing(self, user_content: str) -> bool:
        s = (user_content or "").strip()
        if len(s) < int(self._cfg.get("min_write_chars", 24)):
            return False
        return not s.startswith(_COMMAND_PREFIXES) and not _is_low_information_input(s)

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
        if not (self._enabled and self._writable and self._ingest_client):
            return
        if not self._worth_writing(user_content) or not (assistant_content or "").strip():
            return
        if self._is_recursive_capture(messages):
            return
        resolved_session = session_id or self._session_id or "hermes"
        message_count = len(messages or [])
        event_id = self._ingest_module.stable_event_id(
            "hermes",
            resolved_session,
            message_count,
            user_content,
            assistant_content,
        )
        payload = {
            "event_id": event_id,
            "session_id": resolved_session,
            "turn_id": f"turn-{message_count or event_id[-12:]}",
            "user_message": user_content[:6000],
            "assistant_message": assistant_content[:6000],
            "safe_context": {"runtime": "hermes", "platform": self._platform},
        }
        try:
            self._ingest_client.enqueue("/ingest/turn", payload)
        except Exception as e:
            logger.error("MindMemOS durable turn spool 写入失败: %s", str(e)[:200])

    def on_memory_write(
        self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Mirror explicit builtin-memory writes through the trusted collector."""
        if not (self._enabled and self._writable and self._ingest_client) or action == "remove":
            return
        if not content or len(content.strip()) < 8:
            return
        if isinstance(metadata, dict) and metadata.get("mindmemos_capture"):
            return
        event_id = self._ingest_module.stable_event_id("hermes-memory", self._session_id, action, target, content)
        try:
            self._ingest_client.enqueue(
                "/ingest/memory",
                {
                    "event_id": event_id,
                    "session_id": f"hermes-builtin-{target}",
                    "content": content[:6000],
                    "safe_context": {
                        "runtime": "hermes",
                        "kind": target.upper(),
                        "authority": "high",
                    },
                },
            )
        except Exception as e:
            logger.error("MindMemOS durable builtin-memory spool 写入失败: %s", str(e)[:200])

    # ---------------------------------------------------------------- 模型工具

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "recall",
                "description": (
                    "检索长期记忆库（MindMemOS），查过往项目历史、决策、踩过的坑、用户偏好。"
                    "每轮已自动召回相关记忆；仅当需要补充查询特定主题时才主动调用。"
                    "查不到会明确返回空，不要把无关结果当答案。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要查什么，用自然语言"},
                        "top_k": {"type": "integer", "description": "返回条数，默认 10"},
                    },
                    "required": ["query"],
                },
            }
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "recall":
            return tool_error(f"Unknown tool: {tool_name}")
        if not self._enabled:
            return tool_error("MindMemOS 未配置")
        q = (args.get("query") or "").strip()
        if not q:
            return tool_error("query 不能为空")
        mems = self._search(q, top_k=int(args.get("top_k") or 10), timeout=30.0)
        return json.dumps(
            {
                "success": True,
                "count": len(mems),
                "memories": [
                    {"memory": m.get("memory"), "type": m.get("memory_type"), "time": m.get("last_update_at")}
                    for m in mems
                ],
                "note": "没有结果说明库里确实没有相关内容，不要编造。" if not mems else "",
            },
            ensure_ascii=False,
        )

    # ---------------------------------------------------------------- 配置向导

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "MindMemOS API Key",
                "secret": True,
                "required": True,
                "env_var": "MINDMEMOS_API_KEY",
            },
            {
                "key": "base_url",
                "description": "MindMemOS API 地址",
                "default": "http://127.0.0.1:8000",
                "env_var": "MINDMEMOS_BASE_URL",
            },
            {
                "key": "ingest_key",
                "description": "MindMemOS 当前 Hermes 实例专属写 Key",
                "secret": True,
                "required": True,
                "env_var": "MINDMEMOS_INGEST_KEY",
            },
            {
                "key": "ingest_url",
                "description": "MindMemOS durable collector 地址",
                "default": "http://127.0.0.1:8765",
                "env_var": "MINDMEMOS_INGEST_URL",
            },
            {"key": "user_id", "description": "记忆归属 user_id", "default": "leway"},
            {"key": "top_k", "description": "每轮自动注入的记忆条数", "default": "6"},
            {"key": "score_threshold", "description": "相关度阈值 0-1，低于此值不注入", "default": "0.1"},
            {
                "key": "prefetch_rerank",
                "description": "自动召回是否执行 rerank；手动 recall 始终执行",
                "default": "true",
                "choices": ["true", "false"],
            },
            {"key": "prefetch_timeout", "description": "自动召回单请求超时秒数", "default": "6"},
            {"key": "prefetch_parallelism", "description": "多问句并行检索数，最大 3", "default": "1"},
            {
                "key": "write_enabled",
                "description": "每轮对话自动写入记忆库",
                "default": "true",
                "choices": ["true", "false"],
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = os.path.join(hermes_home, "mindmemos.json")
        cur = {}
        if os.path.exists(path):
            try:
                cur = json.load(open(path, encoding="utf-8"))
            except Exception:
                cur = {}
        cur.update({k: v for k, v in values.items() if v not in (None, "")})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        os.chmod(path, 0o600)

    def backup_paths(self) -> List[str]:
        home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return [os.path.join(home, "mindmemos.json")]


def register(ctx) -> None:
    """注册 MindMemOS 为 memory provider。"""
    ctx.register_memory_provider(MindMemOSProvider())
