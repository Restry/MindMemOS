#!/usr/bin/env python3
"""MindMemOS 统一查看面板 — 单进程后端 + 内嵌前端。

后端做三件事：
  1. 代理 MindMemOS /v1/memory/search（隐藏 API key，规避 CORS）
  2. 直读 Qdrant scroll 做「浏览全部记忆」和统计
  3. 提供静态页面

启动：python3 /Users/leway/Projects/mm-panel/server.py
访问：http://192.168.1.246:8666
"""
import hashlib
import ipaddress
import json
import os
import re
import shutil
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import yaml

MM_API = os.getenv("MINDMEMOS_API", "http://127.0.0.1:8000")
QDRANT = os.getenv("MINDMEMOS_QDRANT_URL", "http://127.0.0.1:6333")
NEO4J = os.getenv("MINDMEMOS_NEO4J_HTTP_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_AUTH = (
    os.getenv("MINDMEMOS_NEO4J_USERNAME", "neo4j"),
    os.getenv("MINDMEMOS_NEO4J_PASSWORD", "mindmemos_dev_password"),
)
COLL = os.getenv("MINDMEMOS_MEMORY_COLLECTION", "memory_item_v1")
ENT_COLL = os.getenv("MINDMEMOS_ENTITY_COLLECTION", "entity_item_v1")
KEYS_PATH = os.path.expanduser(os.getenv("MINDMEMOS_PANEL_KEYS", "/tmp/mm_keys.json"))
KEYS = json.load(open(KEYS_PATH, encoding="utf-8"))
MM_KEY = KEYS['vanilla']
USER_ID = os.getenv("MINDMEMOS_USER", "leway")
HOST = os.getenv("MM_PANEL_HOST", "0.0.0.0")
PORT = int(os.getenv("MM_PANEL_PORT", "8666"))
HERE = os.path.dirname(os.path.abspath(__file__))
BEIJING_TZ = ZoneInfo('Asia/Shanghai')
STATE_DIR = os.path.expanduser(os.getenv("MINDMEMOS_STATE_DIR", "~/.hermes"))
PINNED_PATH = os.path.expanduser(os.getenv("MINDMEMOS_PINNED", os.path.join(STATE_DIR, "mindmemos_pinned.md")))
LEDGER_PATH = os.path.expanduser(os.getenv("MM_TURN_LEDGER", os.path.join(STATE_DIR, "mindmemos_turn_ingest.sqlite3")))
PANEL_VERSION_PATH = os.path.expanduser(
    os.getenv("MM_PANEL_VERSION_PATH", os.path.join(STATE_DIR, "mm_panel_data.version"))
)
CLIENT_CONFIG_PATH = os.path.expanduser(
    os.getenv("MINDMEMOS_CLIENT_CONFIG", os.path.join(STATE_DIR, "mindmemos.json"))
)
LEGACY_TOKEN_PATH = os.path.expanduser(
    os.getenv("MM_MCP_LEGACY_TOKEN", os.path.join(STATE_DIR, "mindmemos_mcp_token"))
)
MODEL_CONFIG_PATH = os.path.expanduser(
    os.getenv(
        "MM_MODEL_CONFIG_PATH",
        os.getenv(
            "MINDMEMOS_CONFIG_PATH",
            "~/Projects/MindMemOS/config/mindmemos/dev.yaml",
        ),
    )
)
MODEL_CONFIG_BACKUP_DIR = os.path.expanduser(
    os.getenv(
        "MM_MODEL_CONFIG_BACKUP_DIR",
        os.path.join(STATE_DIR, "model-config-backups"),
    )
)
MODEL_RELOAD_COMMAND = os.getenv("MM_MODEL_RELOAD_COMMAND", "").strip()
MODEL_ROUTERS = {
    "llm": "chat_model_router",
    "embedding": "embed_model_router",
    "rerank": "rerank_model_router",
}
_MODEL_SETTINGS_LOCK = threading.Lock()


def _model_config() -> dict:
    try:
        with open(MODEL_CONFIG_PATH, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"模型配置文件不存在：{MODEL_CONFIG_PATH}") from exc
    except yaml.YAMLError as exc:
        raise ValueError("模型配置文件不是有效 YAML") from exc
    if not isinstance(data, dict):
        raise ValueError("模型配置文件顶层必须是对象")
    return data


def _model_endpoint(config: dict, kind: str) -> dict:
    router = config.get(MODEL_ROUTERS[kind])
    endpoints = router.get("endpoints") if isinstance(router, dict) else None
    if not isinstance(endpoints, list) or not endpoints or not isinstance(endpoints[0], dict):
        raise ValueError(f"{kind} 缺少可编辑的第一个 endpoint")
    return endpoints[0]


def _public_model_settings(config: dict | None = None) -> dict:
    data = config or _model_config()
    models = {}
    for kind in MODEL_ROUTERS:
        endpoint = _model_endpoint(data, kind)
        models[kind] = {
            "model": str(endpoint.get("model") or ""),
            "endpoint": str(endpoint.get("api_base") or ""),
            "key_configured": bool(str(endpoint.get("api_key") or "").strip()),
        }
    return {"ok": True, "models": models, "config_path": MODEL_CONFIG_PATH}


def _validated_model_value(kind: str, value, current: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} 配置必须是对象")
    model = str(value.get("model") or "").strip()
    endpoint = str(value.get("endpoint") or "").strip().rstrip("/")
    api_key = str(value.get("api_key") or "").strip()
    if not model or len(model) > 300 or any(ch in model for ch in "\r\n\t"):
        raise ValueError(f"{kind} 模型名不能为空，且不能包含换行")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(endpoint) > 2048
    ):
        raise ValueError(f"{kind} Endpoint 必须是有效的 HTTP(S) API 地址，不能包含账号、查询或片段")
    if len(api_key) > 8192 or any(ch in api_key for ch in "\r\n"):
        raise ValueError(f"{kind} API Key 格式不正确")
    if not api_key:
        api_key = str(current.get("api_key") or "")
    if not api_key:
        raise ValueError(f"{kind} 尚未配置 API Key，请填写")
    return {"model": model, "api_base": endpoint, "api_key": api_key}


def _write_yaml_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mindmemos-models.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False, width=120)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _reload_model_services() -> dict:
    if not MODEL_RELOAD_COMMAND:
        return {"reloaded": False, "reload_message": "配置已保存；当前环境未配置自动重载命令"}
    try:
        result = subprocess.run(
            shlex.split(MODEL_RELOAD_COMMAND),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"模型服务重载失败：{type(exc).__name__}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-500:]
        raise RuntimeError(f"模型服务重载失败（exit {result.returncode}）：{detail}")
    return {"reloaded": True, "reload_message": "API 与 MCP 已重载"}


def _save_model_settings(payload: dict) -> dict:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict) or set(models) != set(MODEL_ROUTERS):
        raise ValueError("models 必须完整包含 llm、embedding、rerank")
    with _MODEL_SETTINGS_LOCK:
        config = _model_config()
        for kind in MODEL_ROUTERS:
            target = _model_endpoint(config, kind)
            target.update(_validated_model_value(kind, models[kind], target))

        os.makedirs(MODEL_CONFIG_BACKUP_DIR, mode=0o700, exist_ok=True)
        os.chmod(MODEL_CONFIG_BACKUP_DIR, 0o700)
        stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S-%f")
        stem = os.path.splitext(os.path.basename(MODEL_CONFIG_PATH))[0]
        backup = os.path.join(MODEL_CONFIG_BACKUP_DIR, f"{stem}.{stamp}.yaml")
        shutil.copy2(MODEL_CONFIG_PATH, backup)
        os.chmod(backup, 0o600)
        _write_yaml_atomic(MODEL_CONFIG_PATH, config)
        try:
            reload_result = _reload_model_services()
        except Exception:
            shutil.copy2(backup, MODEL_CONFIG_PATH)
            os.chmod(MODEL_CONFIG_PATH, 0o600)
            try:
                _reload_model_services()
            except Exception:
                pass
            raise
        return {
            **_public_model_settings(config),
            **reload_result,
            "backup": os.path.basename(backup),
        }


def _provider_model_ids(endpoint: str, api_key: str) -> set[str]:
    url = endpoint.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(2_000_000)
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("API Key 无效或无权读取模型列表") from exc
        if exc.code == 404:
            raise ValueError("Endpoint 没有 /models 接口，请确认它是 OpenAI 兼容地址") from exc
        raise ValueError(f"Endpoint 返回 HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise ValueError("Endpoint 连接失败，请检查地址、网络和服务状态") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Endpoint 返回的不是有效模型列表") from exc
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = data.get("models") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Endpoint 返回中没有 data/models 模型列表")
    return {
        str(row.get("id") or row.get("model") or "").strip()
        for row in rows
        if isinstance(row, dict) and (row.get("id") or row.get("model"))
    }


def _test_model_settings(payload: dict) -> dict:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict) or set(models) != set(MODEL_ROUTERS):
        raise ValueError("models 必须完整包含 llm、embedding、rerank")
    config = _model_config()
    prepared = {}
    for kind in MODEL_ROUTERS:
        current = _model_endpoint(config, kind)
        prepared[kind] = _validated_model_value(kind, models[kind], current)
    cache: dict[tuple[str, str], tuple[set[str] | None, str | None]] = {}
    results = {}
    for kind, item in prepared.items():
        cache_key = (item["api_base"], item["api_key"])
        if cache_key not in cache:
            try:
                cache[cache_key] = (_provider_model_ids(*cache_key), None)
            except ValueError as exc:
                cache[cache_key] = (None, str(exc))
        model_ids, error = cache[cache_key]
        if error:
            results[kind] = {"ok": False, "message": error}
            continue
        requested = item["model"]
        provider_model = requested.split("/", 1)[1] if "/" in requested else requested
        matched = requested in model_ids or provider_model in model_ids
        results[kind] = {
            "ok": matched,
            "message": (
                f"连接成功，已找到模型 {provider_model}"
                if matched
                else f"连接成功，但模型列表中没有 {provider_model}"
            ),
        }
    return {"ok": all(item["ok"] for item in results.values()), "results": results}


def _beijing_day(value) -> str:
    """把存储层 UTC 时间归到北京时间自然日；无法解析时返回空串。"""
    if value is None:
        return ''
    s = str(value).strip()
    if not s:
        return ''
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        # MindMemOS 历史数据里的无 offset 时间也是 UTC；明确补上，禁止按机器本地时区猜。
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        return ''


def _recent_snapshot(rows: list[dict], *, now: datetime | None = None, days: int = 14) -> dict:
    """Return one Beijing-time recent view with explicit zero-count calendar days."""
    reference = now or datetime.now(BEIJING_TZ)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=BEIJING_TZ)
    today = reference.astimezone(BEIJING_TZ).date()
    dated = [row for row in rows if row.get('created')]
    dated.sort(key=lambda row: str(row['created']), reverse=True)
    counts = Counter(day for day in (_beijing_day(row['created']) for row in dated) if day)
    by_day = {
        (today - timedelta(days=offset)).isoformat(): counts.get(
            (today - timedelta(days=offset)).isoformat(), 0
        )
        for offset in range(max(1, days))
    }
    return {"today": today.isoformat(), "items": dated[:60], "by_day": by_day}


def _read_rules() -> list[str]:
    try:
        raw = open(PINNED_PATH, encoding='utf-8').read()
    except FileNotFoundError:
        return []
    return [block.strip() for block in raw.split('\n§\n') if len(block.strip()) >= 8]


def _write_rules(rules: list[str]) -> None:
    """Validate and atomically replace the pinned behavior rules."""
    cleaned = []
    for value in rules:
        rule = str(value).strip()
        if not rule:
            continue
        if len(rule) < 8:
            raise ValueError('每条准则至少 8 个字符')
        if len(rule) > 2000:
            raise ValueError('单条准则不要超过 2000 个字符')
        if '\n§\n' in rule:
            raise ValueError('准则内容不能包含分隔符 §')
        cleaned.append(rule)
    if len(cleaned) > 80:
        raise ValueError('行为准则最多 80 条')
    os.makedirs(os.path.dirname(PINNED_PATH), exist_ok=True)
    if os.path.exists(PINNED_PATH):
        shutil.copy2(PINNED_PATH, PINNED_PATH + '.previous')
    fd, tmp = tempfile.mkstemp(prefix='.mindmemos_pinned.', dir=os.path.dirname(PINNED_PATH), text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write('\n§\n'.join(cleaned) + ('\n' if cleaned else ''))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, PINNED_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _bump_data_version() -> None:
    os.makedirs(os.path.dirname(PANEL_VERSION_PATH), exist_ok=True)
    with open(PANEL_VERSION_PATH, 'w', encoding='ascii') as handle:
        handle.write(str(datetime.now(timezone.utc).timestamp()))
    os.chmod(PANEL_VERSION_PATH, 0o600)


def _data_version() -> dict:
    """Return revisions for successful memory commits and panel mutations only."""
    memory_revision = 0.0
    ledger_path = getattr(provenance_ledger, 'path', LEDGER_PATH) if provenance_ledger else LEDGER_PATH
    try:
        with sqlite3.connect(ledger_path) as connection:
            row = connection.execute(
                'SELECT COALESCE(MAX(updated_at), 0) FROM memory_lineage'
            ).fetchone()
            memory_revision = float(row[0] or 0)
    except (OSError, sqlite3.Error):
        pass
    try:
        panel_revision = os.stat(PANEL_VERSION_PATH).st_mtime_ns
    except OSError:
        panel_revision = 0
    return {
        'memory_revision': memory_revision,
        'panel_revision': panel_revision,
        'version': f'{memory_revision:.6f}:{panel_revision}',
    }


def _lan_ip() -> str:
    """本机内网 IP，用于告诉另一台机器该连哪里。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))   # 不实际发包，只为拿到出口网卡地址
        return s.getsockname()[0]
    except Exception:
        return "192.168.1.246"
    finally:
        s.close()


LAN_IP = os.getenv("MINDMEMOS_LAN_IP") or _lan_ip()




# ---- token 与 provenance：共用 MindMemOS 实现，不在面板复制逻辑 ----
import importlib.util as _ilu  # noqa: E402

_MM_ROOT = os.path.expanduser(os.getenv('MINDMEMOS_ROOT', '~/Projects/MindMemOS'))


def _load_module(name: str, path: str):
    spec = _ilu.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = _ilu.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


try:
    mcp_tokens = _load_module('mm_panel_tokens', os.path.join(_MM_ROOT, 'mcp_tokens.py'))
except Exception as _e:  # 面板不该因为这个起不来
    mcp_tokens = None
    print(f'⚠️  token 模块加载失败（{_e}），令牌管理页不可用', file=sys.stderr)

try:
    turn_ingest = _load_module('mm_panel_turn_ingest', os.path.join(_MM_ROOT, 'turn_ingest.py'))
    provenance_ledger = turn_ingest.TurnLedger()
except Exception as _e:
    turn_ingest = None
    provenance_ledger = None
    print(f'⚠️  provenance 模块加载失败（{_e}），来源标签不可用', file=sys.stderr)

_PANEL_INSTANCE = (os.getenv('MM_PANEL_INSTANCE') or socket.gethostname().split('.')[0]).lower()
PANEL_IMPORT_PRINCIPAL = {
    'client_id': f'mm-panel-{_PANEL_INSTANCE}',
    'agent_kind': 'operator',
    'instance': _PANEL_INSTANCE,
    'credential_id': 'local-panel',
    'display_name': 'Panel import',
    'scope': 'write',
    'authority': 'local_panel',
}


def _is_lan(addr: str) -> bool:
    """Allow direct localhost/RFC1918/ULA callers; never trust forwarding headers."""

    if addr == 'localhost':
        return True
    try:
        ip = ipaddress.ip_address(addr.split('%', 1)[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    networks = (
        ipaddress.ip_network('10.0.0.0/8'),
        ipaddress.ip_network('172.16.0.0/12'),
        ipaddress.ip_network('192.168.0.0/16'),
        ipaddress.ip_network('fc00::/7'),
    )
    return any(ip in network for network in networks)


def _install_script() -> str:
    """Generate a client-neutral preflight for one Agent instance."""

    return f'''#!/usr/bin/env bash
# MindMemOS single-instance connection preflight.
# This script never guesses or edits a client configuration path.
set -euo pipefail

MCP_URL="http://{LAN_IP}:8765/mcp"
SKILL_URL="http://{LAN_IP}:{PORT}/skills/mindmemos-memory.md"
: "${{MINDMEMOS_MCP_TOKEN:?请设置当前 Agent 实例专属的 MINDMEMOS_MCP_TOKEN}}"

if [ -n "${{MINDMEMOS_SKILL_DIR:-}}" ]; then
    mkdir -p "$MINDMEMOS_SKILL_DIR"
    curl -fsSL "$SKILL_URL" -o "$MINDMEMOS_SKILL_DIR/SKILL.md"
    echo "Skill written to caller-selected MINDMEMOS_SKILL_DIR"
else
    echo "Skill not installed: set MINDMEMOS_SKILL_DIR to the location chosen by this runtime"
fi

curl -fsS --max-time 20 -X POST "$MCP_URL" \
  -H "Authorization: Bearer $MINDMEMOS_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{}}}}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "instructions" in d.get("result", {{}})'

cat <<'EOF'
MindMemOS connection verified.
Name: mindmemos
Transport: Streamable HTTP
Endpoint: http://{LAN_IP}:8765/mcp
Authentication: HTTP bearer Key from the runtime secret store
Companion Skill: http://{LAN_IP}:{PORT}/skills/mindmemos-memory.md

No client configuration was modified. Register the endpoint and Skill through
the current runtime's own configuration and extension mechanisms.
EOF
'''

# whoami 结果缓存：(写入时间, 响应体)。5 次检索要几秒，没必要每次点都重算
_WHO_CACHE = None

_EXTRACTOR = None


def _parse_multipart(body: bytes, content_type: str):
    """解析 multipart/form-data，返回 (文件列表, 普通字段)。

    为什么手写：Python 3.13 起标准库移除了 cgi 模块（PEP 594），
    而面板跑在 3.14 上。只需要文件+文本字段这点功能，不值得引依赖。
    """
    m = re.search(r'boundary=("?)([^";]+)\1', content_type or '')
    if not m:
        raise ValueError('缺少 multipart boundary')
    sep = b'--' + m.group(2).encode()
    files, fields = [], {}
    for part in body.split(sep):
        if not part.strip() or part.strip() == b'--':
            continue
        if b'\r\n\r\n' not in part:
            continue
        head, data = part.split(b'\r\n\r\n', 1)
        data = data.rstrip(b'\r\n')
        h = head.decode('utf-8', 'ignore')
        nm = re.search(r'name="([^"]*)"', h)
        fn = re.search(r'filename="([^"]*)"', h)
        if not nm:
            continue
        if fn and fn.group(1):
            files.append((fn.group(1), data))
        else:
            fields[nm.group(1)] = data.decode('utf-8', 'ignore').strip()
    return files, fields


def _load_extractor():
    """加载 MindMemOS/scripts/ingest/extractor.py。

    面板跑在系统 Python，而 docx/pypdf/pptx 装在 MM 的 venv 里
    （系统 Python 受 PEP 668 保护装不了），所以要把 venv 的
    site-packages 挂进 sys.path 才 import 得到。
    """
    global _EXTRACTOR
    if _EXTRACTOR is not None:
        return _EXTRACTOR
    import glob
    import importlib.util
    mm = os.path.expanduser('~/Projects/MindMemOS')
    for sp in glob.glob(os.path.join(mm, '.venv/lib/python*/site-packages')):
        if sp not in sys.path:
            sys.path.append(sp)
    path = os.path.join(mm, 'scripts/ingest/extractor.py')
    spec = importlib.util.spec_from_file_location('mm_extractor', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'找不到 {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _EXTRACTOR = mod
    return mod

# 实体来源有两路：LLM（vanilla_llm）和 spaCy NER。
# LLM 用下面这套干净类型；spaCy 产出 cardinal/date/term/norp 等噪音
# （"06"、"1"、"2026" 这种纯数字度数还最高），默认只画 LLM 类型。
LLM_TYPES = {"person", "organization", "location", "project", "product",
             "tool", "file", "model", "version", "other"}
NOISY_TYPES = {"cardinal", "date", "ordinal", "time", "percent", "money",
               "quantity", "term", "technical_term", "norp", "work",
               "language", "code", "proper_noun", "acronym", "quoted_text"}


def cypher(query, params=None):
    """跑一条 Cypher，返回 [{列名: 值}]。"""
    import base64
    body = {"statements": [{"statement": query, "parameters": params or {}}]}
    token = base64.b64encode(f"{NEO4J_AUTH[0]}:{NEO4J_AUTH[1]}".encode()).decode()
    req = urllib.request.Request(
        NEO4J, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    if d.get("errors"):
        raise RuntimeError(d["errors"][0].get("message", "cypher error"))
    res = d["results"][0]
    cols = res["columns"]
    return [dict(zip(cols, row["row"])) for row in res["data"]]


def http_json(url, payload=None, headers=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def scroll_all(coll, limit=10000, with_payload=True):
    """拉全量点（分页直到取完）。"""
    out, offset = [], None
    while len(out) < limit:
        body = {"limit": min(512, limit - len(out)), "with_payload": with_payload,
                "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        r = http_json(f"{QDRANT}/collections/{coll}/points/scroll", body)['result']
        pts = r.get('points', [])
        out.extend(pts)
        offset = r.get('next_page_offset')
        if not offset or not pts:
            break
    return out

def _attach_provenance(rows):
    memory_ids = [str(row.get('id') or row.get('memory_id') or '') for row in rows]
    found = provenance_ledger.provenance_for(memory_ids) if provenance_ledger else {}
    for row, memory_id in zip(rows, memory_ids):
        provenance = found.get(memory_id)
        if provenance is None and row.get('app_id'):
            agent_kind, _, instance = str(row.get('agent_id') or 'legacy:unknown').partition(':')
            contributor = {
                'client_id': row['app_id'],
                'agent_kind': agent_kind or 'legacy',
                'instance': instance or 'unknown',
                'display_name': instance or row['app_id'],
                'authority': 'historical_payload',
                'last_capture_mode': 'unknown',
                'capture_modes': ['unknown'],
            }
            provenance = {
                'origin': {**contributor, 'capture_mode': 'unknown'},
                'last_source': {**contributor, 'capture_mode': 'unknown'},
                'contributors': [contributor],
            }
        row['provenance'] = provenance
    return rows


def clean_memories(points):
    """只保留真正的记忆条目（有 content 的），并标注噪音。"""
    rows = []
    for p in points:
        pl = p.get('payload', {})
        c = pl.get('content')
        if not c:
            continue
        md = pl.get('metadata', {}) or {}
        doc = md.get('doc') or '(未标注)'
        # 噪音判定：内容主要在描述文档路径/分片位置本身，而非真实知识
        noise = ('长期档案' in c and ('路径' in c or '文档' in c or '/' in c)) \
            or ('部分' in c and ('第' in c[:40] or '/' in c[:40])) \
            or c.strip().startswith('以下是')
        rows.append({
            "id": pl.get('memory_id'),
            "content": c,
            "type": pl.get('mem_type') or 'unknown',
            "doc": doc,
            # 保留完整 ISO 时区；前端统一格式化为 Asia/Shanghai。
            "created": str(pl.get('created_at') or ''),
            "entities": (md.get('entities') or [])[:6],
            "noise": bool(noise),
            "app_id": pl.get('app_id'),
            "agent_id": pl.get('agent_id'),
        })
    _attach_provenance(rows)
    rows.sort(key=lambda r: r['created'], reverse=True)
    return rows


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            b = open(os.path.join(HERE, 'index.html'), 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == '/lucide.js':
            # 本地托管，不依赖外网 CDN（内网机器可能上不了外网）
            try:
                b = open(os.path.join(HERE, 'lucide.js'), 'rb').read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == '/bootstrap.json':
            # 只对内网直连返回凭据；经 223 反代进来的外网请求一律 403。
            # 外网机器接入的正确路径是：人工在面板生成 token → 手动填进客户端。
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False,
                            "error": "bootstrap 只在内网可用；请在面板「访问令牌」页生成 token"}, 403)
                return
            try:
                cfg = json.load(open(CLIENT_CONFIG_PATH, encoding='utf-8'))
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return
            try:
                mcp_token = open(LEGACY_TOKEN_PATH, encoding='utf-8').read().strip()
            except Exception:
                mcp_token = ''
            self._send({
                "ok": True,
                "base_url": f"http://{LAN_IP}:8000",
                "api_key": cfg.get("api_key", ""),
                "user_id": cfg.get("user_id", USER_ID),
                "top_k": cfg.get("top_k", 6),
                "score_threshold": cfg.get("score_threshold", 0.1),
                "write_enabled": cfg.get("write_enabled", True),
                "min_write_chars": cfg.get("min_write_chars", 24),
                # 远程 MCP（HTTP transport）：别的电脑不用装脚本，填 URL 即可
                "mcp_url": f"http://{LAN_IP}:8765/mcp",
                "mcp_token": mcp_token,
            })
            return
        if path in ('/migrate.py', '/mcp_server.py', '/ingest.py'):
            # 让另一台机器能直接 curl 下载，不用 scp
            src = {
                '/migrate.py': os.path.expanduser(
                    '~/Projects/MindMemOS/migrate_hermes_to_mm.py'),
                '/mcp_server.py': os.path.expanduser(
                    '~/Projects/MindMemOS/mcp_server.py'),
                '/ingest.py': os.path.expanduser(
                    '~/Projects/MindMemOS/scripts/ingest/cli.py'),
            }[path]
            try:
                b = open(src, 'rb').read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path.startswith('/skills/'):
            # 对外分发 skill，跟 llms.txt 同级。别的机器不用 clone 仓库。
            name = os.path.basename(path[len('/skills/'):])
            if not name.endswith('.md') or '/' in name or '..' in name:
                self.send_error(404)
                return
            try:
                b = open(os.path.join(HERE, 'skills', name), 'rb').read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/markdown; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if path == '/install.sh':
            # 内嵌 token，因此跟 bootstrap.json 同级：只在内网发。
            if not _is_lan(self.client_address[0]):
                self.send_error(403, 'install.sh is LAN-only')
                return
            b = _install_script().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/x-shellscript; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if path == '/api/tokens':
            # 列出已签发的 token（只有元数据，没有明文也没有 hash）
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            if mcp_tokens is None:
                self._send({"ok": False, "error": "token 模块未加载"}, 500)
                return
            self._send({"ok": True, "tokens": mcp_tokens.listing(),
                        "mcp_url": f"http://{LAN_IP}:8765/mcp"})
            return

        if path == '/api/models':
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                self._send(_public_model_settings())
            except ValueError as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == '/llms.txt':
            # 给 AI 客户端看的站点说明（llmstxt.org 约定）
            try:
                b = open(os.path.join(HERE, 'llms.txt'), 'rb').read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == '/api/suggest':
            # 建议问题动态生成：从图谱取高频实体 + 最近活跃实体，
            # 拼成自然问句。写死的示例会随着项目演进过期。
            import random
            try:
                hot = cypher(
                    "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                    "WHERE e.entity_type IN ['project','tool','product'] "
                    "AND size(e.entity_name) < 24 "
                    "RETURN e.entity_name AS n, count(m) AS c "
                    "ORDER BY c DESC LIMIT 24")
                fresh = cypher(
                    "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                    "WHERE e.entity_type IN ['project','tool'] "
                    "AND size(e.entity_name) < 24 AND m.created_at IS NOT NULL "
                    "RETURN e.entity_name AS n, max(m.created_at) AS last "
                    "ORDER BY last DESC LIMIT 12")
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return

            # 问句模板：覆盖"是什么/限制/决策/踩坑"几类真实会问的角度
            tpl = ["{n} 是什么", "{n} 有什么已知限制", "{n} 的架构是怎样的",
                   "{n} 有什么约束和铁律", "{n} 踩过什么坑", "{n} 用了哪些技术"]
            names_hot = [r["n"] for r in hot if r.get("n")]
            names_new = [r["n"] for r in fresh if r.get("n")]

            picked, seen = [], set()
            # 最近活跃的优先露出，方便接着上次的事往下问；
            # 数量不够时再从高频池里补，保证始终有 8 条
            ordered = names_new[:6] + [n for n in names_hot if n not in names_new[:6]]
            for n in ordered:
                if n in seen:
                    continue
                seen.add(n)
                picked.append(random.choice(tpl).format(n=n))
                if len(picked) >= 8:
                    break
            random.shuffle(picked)
            self._send({"ok": True, "suggestions": picked,
                        "pool": len(set(names_hot) | set(names_new))})
            return
        if path == '/api/whoami':
            # 用户画像：把散在各处的身份信息按维度聚合。
            # 跟 MCP 的 whoami 工具同一套逻辑，面板上也能直接看。
            # 走 5 次检索要几秒，加个 10 分钟缓存（?fresh=1 强制刷新）
            import time as _t
            global _WHO_CACHE
            fresh = 'fresh=1' in (self.path.split('?', 1)[1] if '?' in self.path else '')
            if not fresh and _WHO_CACHE and _t.time() - _WHO_CACHE[0] < 600:
                self._send(_WHO_CACHE[1])
                return
            dims = [
                ("称呼与身份", "用户的称呼、姓名、别名、时区与语言偏好"),
                ("家庭", "用户的儿子 女儿 配偶 妹妹 家人 健康状况 家庭收入"),
                ("工作", "用户就职的公司 部门 岗位职责 日常会议"),
                ("环境与设备", "用户的电脑 服务器 内网地址 硬件配置"),
                ("协作偏好", "用户偏好的沟通方式、汇报格式、禁止事项"),
            ]
            out, seen = [], set()
            for title, q in dims:
                try:
                    d = http_json(f"{MM_API}/v1/memory/search", {
                        "user_id": USER_ID, "query": q, "top_k": 4,
                        # 身份类查询开 rerank 反而把技术记忆排前面
                        "rerank": False, "score_threshold": 0.05,
                    }, headers={"Authorization": f"Bearer {MM_KEY}"})
                    mems = (d.get("data") or {}).get("memories") or []
                except Exception as e:
                    out.append({"title": title, "items": [f"（检索失败：{e}）"]})
                    continue
                items = []
                for m in mems:
                    t = (m.get("memory") or "").strip()
                    # 画像应该是简短事实。超长的多半是会议纪要/汇报正文
                    # 被误召回，放进画像里只会淹没真正有用的信息
                    if not t or t in seen or len(t) > 160:
                        continue
                    seen.add(t)
                    items.append(t)
                out.append({"title": title, "items": items or ["（暂无）"]})

            rules = _read_rules()
            _resp = {"ok": True, "dims": out, "rules": rules}
            _WHO_CACHE = (_t.time(), _resp)
            self._send(_resp)
            return
        if path == '/api/all':
            snapshot_version = _data_version()
            try:
                pts = scroll_all(COLL)
                rows = clean_memories(pts)
                ents = http_json(f"{QDRANT}/collections/{ENT_COLL}", None)['result']
                stats = {
                    "memories": len(rows),
                    "noise": sum(1 for r in rows if r['noise']),
                    "entities": ents.get('points_count', 0),
                    "by_type": dict(Counter(r['type'] for r in rows).most_common()),
                    "by_doc": dict(Counter(r['doc'] for r in rows).most_common()),
                }
                self._send({"ok": True, "rows": rows, "stats": stats,
                            "recent": _recent_snapshot(rows), **snapshot_version})
            except Exception as e:
                self._send({"ok": False, "error": str(e)[:300]}, 500)
            return
        if path == '/api/health':
            out = {}
            for name, url in [("mindmemos", f"{MM_API}/healthz"),
                              ("qdrant", f"{QDRANT}/healthz"),
                              ("neo4j", "http://127.0.0.1:7474")]:
                try:
                    urllib.request.urlopen(url, timeout=3)
                    out[name] = True
                except Exception:
                    out[name] = False
            self._send(out)
            return
        if path == '/api/version':
            # Cheap local revision check; no Qdrant scan and no queue-state mtime noise.
            self._send({"ok": True, **_data_version()})
            return
        if self.path.startswith('/api/graph'):
            self._graph()
            return
        self._send({"error": "not found"}, 404)

    def _graph(self):
        """返回图谱数据 {nodes, links, types}。

        支持 query 参数：
          focus=<实体名>  只看某个实体的邻域（2 跳）
          limit=<N>       主干实体数量上限，默认 60
          noisy=1         包含数字/日期类噪音实体
        """
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        focus = (q.get('focus', [''])[0] or '').strip().lower()
        limit = min(int(q.get('limit', ['60'])[0] or 60), 200)
        noisy = list(NOISY_TYPES) if q.get('noisy', ['0'])[0] != '1' else ['\x00']

        try:
            if focus:
                # 以某实体为中心，抓它相关的记忆及这些记忆提到的其它实体
                rows = cypher("""
                    MATCH (c:Entity) WHERE toLower(c.entity_name) CONTAINS $focus
                    WITH c LIMIT 3
                    MATCH (c)<-[:MENTIONS]-(m:Memory)-[:MENTIONS]->(e:Entity)
                    WHERE NOT e.entity_type IN $noisy
                    RETURN c.entity_name AS src, c.entity_type AS stype,
                           e.entity_name AS dst, e.entity_type AS dtype,
                           count(DISTINCT m) AS w
                    ORDER BY w DESC LIMIT $lim
                """, {"focus": focus, "noisy": noisy, "lim": limit * 3})
            else:
                # 全局主干：取共现最强的实体对
                rows = cypher("""
                    MATCH (a:Entity)<-[:MENTIONS]-(m:Memory)-[:MENTIONS]->(b:Entity)
                    WHERE NOT a.entity_type IN $noisy AND NOT b.entity_type IN $noisy
                      AND a.entity_name < b.entity_name
                    WITH a, b, count(DISTINCT m) AS w
                    WHERE w >= 2
                    RETURN a.entity_name AS src, a.entity_type AS stype,
                           b.entity_name AS dst, b.entity_type AS dtype, w
                    ORDER BY w DESC LIMIT $lim
                """, {"noisy": noisy, "lim": limit * 3})
        except Exception as e:
            self._send({"ok": False, "error": str(e)[:300]}, 502)
            return

        nodes, links = {}, []
        for r in rows:
            for nm, tp in ((r['src'], r['stype']), (r['dst'], r['dtype'])):
                if nm not in nodes:
                    nodes[nm] = {"id": nm, "type": tp or "term", "deg": 0}
                nodes[nm]["deg"] += r['w']
            links.append({"source": r['src'], "target": r['dst'], "w": r['w']})

        self._send({"ok": True, "nodes": list(nodes.values()), "links": links,
                    "types": sorted({n['type'] for n in nodes.values()}),
                    "focus": focus})

    def do_POST(self):
        path = self.path.split('?')[0]

        if path == '/api/models/test':
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 100_000:
                    raise ValueError('请求体为空或过大')
                body = json.loads(self.rfile.read(n))
                self._send(_test_model_settings(body))
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f'{type(e).__name__}: {e}'}, 500)
            return

        if path == '/api/models':
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 100_000:
                    raise ValueError('请求体为空或过大')
                body = json.loads(self.rfile.read(n))
                self._send(_save_model_settings(body))
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f'{type(e).__name__}: {e}'}, 500)
            return

        if path == '/api/rules':
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 200_000:
                    raise ValueError('请求体为空或过大')
                body = json.loads(self.rfile.read(n))
                rules = body.get('rules')
                if not isinstance(rules, list):
                    raise ValueError('rules 必须是数组')
                _write_rules(rules)
                _bump_data_version()
                global _WHO_CACHE
                _WHO_CACHE = None
                self._send({"ok": True, "rules": _read_rules(), **_data_version()})
            except ValueError as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f'{type(e).__name__}: {e}'}, 500)
            return

        if path in ('/api/tokens/issue', '/api/tokens/revoke'):
            # 签发 / 撤销 MCP token。只允许内网直连——
            # 外网经 223 反代进来的请求源地址不在白名单，天然被挡。
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            if mcp_tokens is None:
                self._send({"ok": False, "error": "token 模块未加载"}, 500)
                return
            try:
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}')
            except Exception as e:
                self._send({"ok": False, "error": f'请求体解析失败: {e}'}, 400)
                return
            try:
                if path.endswith('/issue'):
                    ttl = body.get('ttl_days')
                    rec = mcp_tokens.issue(
                        body.get('name') or 'unnamed',
                        body.get('scope') or 'read',
                        int(ttl) if ttl else None,
                        client_id=body.get('client_id') or None,
                        agent_kind=body.get('agent_kind') or None,
                        instance=body.get('instance') or None,
                        display_name=body.get('display_name') or None)
                    # token 明文只在这里出现一次，之后库里只有 sha256
                    self._send({"ok": True, "token": rec,
                                "mcp_url": f"http://{LAN_IP}:8765/mcp"})
                else:
                    ok = mcp_tokens.revoke(body.get('id') or '')
                    self._send({"ok": ok,
                                "error": None if ok else "找不到该 token 或已撤销"},
                               200 if ok else 404)
            except Exception as e:
                self._send({"ok": False, "error": f'{type(e).__name__}: {e}'}, 500)
            return

        if path == '/api/upload':
            # 界面上传文档。解析复用 MindMemOS/scripts/ingest/extractor.py，
            # 跟 CLI 是同一份实现，不会两边行为不一致。
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0:
                    raise ValueError('空请求体')
                if n > 200 * 1024 * 1024:
                    raise ValueError('单次上传不要超过 200MB')
                body = self.rfile.read(n)
                items, fields = _parse_multipart(
                    body, self.headers.get('Content-Type', ''))
            except Exception as e:
                self._send({"ok": False, "error": f"表单解析失败：{e}"}, 400)
                return

            tag = (fields.get('tag') or '').strip()
            if not items:
                self._send({"ok": False, "error": "没有收到文件"}, 400)
                return

            try:
                ext_mod = _load_extractor()
            except Exception as e:
                self._send({"ok": False,
                            "error": f"抽取模块加载失败：{e}"}, 500)
                return

            results = []
            for fname, raw in items:
                name = os.path.basename(fname or '')
                if not name:
                    continue
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in ext_mod.EXTS:
                    results.append({"file": name, "ok": False,
                                    "detail": f"不支持的格式 {suffix}"})
                    continue
                tmp = ''
                try:
                    with tempfile.NamedTemporaryFile(
                            suffix=suffix, delete=False) as tf:
                        tf.write(raw)
                        tmp = tf.name
                    text = ext_mod.extract(tmp)
                    cs = ext_mod.chunks(text)
                except Exception as e:
                    results.append({"file": name, "ok": False,
                                    "detail": f"抽取失败：{type(e).__name__}"})
                    continue
                finally:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)

                if not cs:
                    results.append({"file": name, "ok": False,
                                    "detail": "内容太少，已跳过"})
                    continue

                label = f"[文档：{name}]" + (f"[{tag}]" if tag else "")
                good = 0
                for chunk_index, ch in enumerate(cs):
                    try:
                        event_id = 'import-' + hashlib.sha256(
                            f'{name}\0{tag}\0{chunk_index}\0{ch}'.encode()
                        ).hexdigest()
                        d = http_json(f"{MM_API}/v1/memory/add", {
                            "user_id": USER_ID,
                            "app_id": PANEL_IMPORT_PRINCIPAL['client_id'],
                            "agent_id": (
                                f"{PANEL_IMPORT_PRINCIPAL['agent_kind']}:"
                                f"{PANEL_IMPORT_PRINCIPAL['instance']}"
                            ),
                            "session_id": f"upload-{hashlib.sha256(name.encode()).hexdigest()[:12]}",
                            "messages": [{"role": "user",
                                          "content": f"{label}\n{ch}"}],
                            "mode": "sync",
                            "metadata": {"provenance": {
                                **PANEL_IMPORT_PRINCIPAL,
                                "capture_mode": "import",
                                "event_id": event_id,
                            }},
                        }, headers={"Authorization": f"Bearer {MM_KEY}"})
                        if str(d.get("code", "")).lower() in ("ok", "0"):
                            if provenance_ledger:
                                provenance_ledger.record_response(
                                    d,
                                    PANEL_IMPORT_PRINCIPAL,
                                    capture_mode='import',
                                    event_id=event_id,
                                )
                            good += 1
                    except Exception:
                        pass
                results.append({"file": name, "ok": good > 0,
                                "detail": f"{len(text)} 字符 → 入库 {good}/{len(cs)} 片"})
            if any(result.get('ok') for result in results):
                _bump_data_version()
            self._send({"ok": True, "results": results})
            return
        if path in ('/api/delete', '/api/update'):
            # 编辑/删除走 MM 官方接口，不直接动 Qdrant——
            # 直接删向量库会漏掉 Neo4j 里的实体和边。
            # delete 是**软删除**（status 置 archived），数据仍在但不再被召回。
            try:
                n = int(self.headers.get('Content-Length') or 0)
                req = json.loads(self.rfile.read(n) or b'{}')
            except Exception as e:
                self._send({"ok": False, "error": f"bad json: {e}"}, 400)
                return
            mid = (req.get('memory_id') or '').strip()
            if not mid:
                self._send({"ok": False, "error": "缺少 memory_id"}, 400)
                return
            body = {"memory_id": mid}
            if path == '/api/update':
                c = (req.get('content') or '').strip()
                if not c:
                    self._send({"ok": False, "error": "内容不能为空"}, 400)
                    return
                body["content"] = c
            ep = '/v1/memory/delete' if path == '/api/delete' else '/v1/memory/update'
            try:
                d = http_json(f"{MM_API}{ep}", body,
                              headers={"Authorization": f"Bearer {MM_KEY}"})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return
            ok = str(d.get("code", "")).lower() in ("ok", "0", "success")
            if ok:
                _bump_data_version()
            self._send({"ok": ok, "detail": d.get("message") or d.get("code")})
            return
        if path != '/api/search':
            self._send({"error": "not found"}, 404)
            return
        n = int(self.headers.get('Content-Length', 0))
        try:
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            req = {}
        payload = {"user_id": req.get('user_id') or USER_ID,
                   "query": req.get('query', ''),
                   "top_k": int(req.get('top_k') or 10),
                   "rerank": True,
                   "score_threshold": float(req.get('score_threshold', 0.1))}
        try:
            r = http_json(f"{MM_API}/v1/memory/search", payload,
                          {"Authorization": f"Bearer {MM_KEY}"})
            mems = r.get('data', {}).get('memories', []) or []
            _attach_provenance(mems)
            self._send({"ok": True, "memories": mems})
        except urllib.error.HTTPError as e:
            self._send({"ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}"}, 502)
        except Exception as e:
            self._send({"ok": False, "error": str(e)[:300]}, 502)


if __name__ == '__main__':
    print(f"MindMemOS 面板  ->  http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
