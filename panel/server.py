#!/usr/bin/env python3
"""MindMemOS 统一查看面板 — 单进程后端 + 内嵌前端。

后端做三件事：
  1. 代理 MindMemOS /v1/memory/search（隐藏 API key，规避 CORS）
  2. 直读 Qdrant scroll 提供「最新新增」完整浏览和统计
  3. 提供静态页面

启动：python3 /Users/leway/Projects/MindMemOS/panel/server.py
访问：http://192.168.1.246:8666
"""

import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import yaml
from recall_evaluation import RecallReviewStore, build_recall_snapshot
from recall_judge import JudgeEndpoint, RecallJudge

MM_API = os.getenv("MINDMEMOS_API", "http://127.0.0.1:8000")
QDRANT = os.getenv("MINDMEMOS_QDRANT_URL", "http://127.0.0.1:6333")
NEO4J = os.getenv("MINDMEMOS_NEO4J_HTTP_URL", "http://127.0.0.1:7474/db/neo4j/tx/commit")
NEO4J_AUTH = (
    os.getenv("MINDMEMOS_NEO4J_USERNAME", "neo4j"),
    os.getenv("MINDMEMOS_NEO4J_PASSWORD", "mindmemos_dev_password"),
)
COLL = os.getenv("MINDMEMOS_MEMORY_COLLECTION", "memory_item_v1")
ENT_COLL = os.getenv("MINDMEMOS_ENTITY_COLLECTION", "entity_item_v1")
SEARCH_COLL = os.getenv("MINDMEMOS_SEARCH_RECORD_COLLECTION", "search_record_v1")
ADD_COLL = os.getenv("MINDMEMOS_ADD_RECORD_COLLECTION", "add_record_v1")
KEYS_PATH = os.path.expanduser(os.getenv("MINDMEMOS_PANEL_KEYS", "/tmp/mm_keys.json"))
PROVIDER_CONFIG_PATH = os.path.expanduser(os.getenv("MINDMEMOS_PROVIDER_CONFIG", "~/.hermes/mindmemos.json"))
RUNTIME_CONFIG_PATH = os.path.expanduser(
    os.getenv(
        "MINDMEMOS_CONFIG_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "mindmemos", "dev.yaml"),
    )
)
PANEL_MEMORY_ALGORITHM = os.getenv("MINDMEMOS_PANEL_MEMORY_ALGORITHM", "vanilla").strip()
PANEL_API_KEY_ID = os.getenv("MINDMEMOS_PANEL_API_KEY_ID", "").strip()


def _runtime_auth_api_key() -> tuple[str, str] | None:
    """Read the same API-key table selected by the running MindMemOS config."""
    config_path = os.path.abspath(RUNTIME_CONFIG_PATH)
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot read MindMemOS runtime auth config: {type(exc).__name__}") from exc

    auth = config.get("auth") if isinstance(config, dict) else None
    if not isinstance(auth, dict) or str(auth.get("mode") or "api_key") != "api_key":
        return None
    key_file = os.path.expanduser(str(auth.get("api_key_file") or "api_keys.yaml"))
    if not os.path.isabs(key_file):
        key_file = os.path.join(os.path.dirname(config_path), key_file)
    key_file = os.path.abspath(key_file)
    try:
        with open(key_file, encoding="utf-8") as handle:
            key_table = yaml.safe_load(handle) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Cannot read configured MindMemOS API key file: {type(exc).__name__}") from exc

    enabled = [
        row
        for row in (key_table.get("api_keys") or [])
        if isinstance(row, dict) and row.get("enabled", True) and str(row.get("api_key") or "").strip()
    ]
    if PANEL_API_KEY_ID:
        matches = [row for row in enabled if str(row.get("key_id") or "") == PANEL_API_KEY_ID]
    else:
        matches = [row for row in enabled if str(row.get("memory_algorithm") or "") == PANEL_MEMORY_ALGORITHM]
    if len(matches) != 1:
        selector = f"key_id={PANEL_API_KEY_ID}" if PANEL_API_KEY_ID else f"memory_algorithm={PANEL_MEMORY_ALGORITHM}"
        raise RuntimeError(f"Configured MindMemOS API key selector must match exactly one enabled key ({selector})")
    return str(matches[0]["api_key"]).strip(), key_file


def _load_memory_api_key() -> tuple[str, str]:
    configured = os.getenv("MINDMEMOS_API_KEY", "").strip()
    if configured:
        return configured, "MINDMEMOS_API_KEY"
    runtime_key = _runtime_auth_api_key()
    if runtime_key:
        return runtime_key
    for path in dict.fromkeys((KEYS_PATH, PROVIDER_CONFIG_PATH)):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            for field in ("vanilla", "api_key", "key"):
                value = str(data.get(field) or "").strip()
                if value:
                    return value, path
    raise RuntimeError("MindMemOS API credential is not configured")


MM_KEY, MM_KEY_SOURCE = _load_memory_api_key()
USER_ID = os.getenv("MINDMEMOS_USER", "leway")
HOST = os.getenv("MM_PANEL_HOST", "0.0.0.0")
PORT = int(os.getenv("MM_PANEL_PORT", "8666"))
HERE = os.path.dirname(os.path.abspath(__file__))
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
STATE_DIR = os.path.expanduser(os.getenv("MINDMEMOS_STATE_DIR", "~/.hermes"))
PINNED_PATH = os.path.expanduser(os.getenv("MINDMEMOS_PINNED", os.path.join(STATE_DIR, "mindmemos_pinned.md")))
LEDGER_PATH = os.path.expanduser(os.getenv("MM_TURN_LEDGER", os.path.join(STATE_DIR, "mindmemos_turn_ingest.sqlite3")))
PANEL_VERSION_PATH = os.path.expanduser(
    os.getenv("MM_PANEL_VERSION_PATH", os.path.join(STATE_DIR, "mm_panel_data.version"))
)
RECALL_REVIEWS_PATH = os.path.expanduser(
    os.getenv("MM_RECALL_REVIEWS", os.path.join(STATE_DIR, "mindmemos_recall_reviews.sqlite3"))
)
RECALL_REVIEWS = RecallReviewStore(RECALL_REVIEWS_PATH)
RECALL_JUDGE_MODEL = os.getenv("MM_RECALL_JUDGE_MODEL", "hub-cloud/gpt-4.1").strip()
RECALL_JUDGE_ENDPOINT_ID = os.getenv("MM_RECALL_JUDGE_ENDPOINT_ID", "ep_71055bd1daa7").strip()
RECALL_JUDGE_RECENT_CALLS = int(os.getenv("MM_RECALL_JUDGE_RECENT_CALLS", "100"))
RECALL_JUDGE_INTERVAL_SECONDS = float(os.getenv("MM_RECALL_JUDGE_INTERVAL_SECONDS", "1800"))
RECALL_JUDGE_ENABLED = os.getenv("MM_RECALL_JUDGE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
CLIENT_CONFIG_PATH = os.path.expanduser(os.getenv("MINDMEMOS_CLIENT_CONFIG", os.path.join(STATE_DIR, "mindmemos.json")))
LEGACY_TOKEN_PATH = os.path.expanduser(os.getenv("MM_MCP_LEGACY_TOKEN", os.path.join(STATE_DIR, "mindmemos_mcp_token")))
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
MODEL_ENDPOINTS_PATH = os.path.expanduser(
    os.getenv("MM_MODEL_ENDPOINTS_PATH", os.path.join(STATE_DIR, "model-endpoints.json"))
)
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
    registry = _endpoint_registry()
    by_url = {item["endpoint"].rstrip("/"): item["id"] for item in registry["endpoints"]}
    models = {}
    for kind in MODEL_ROUTERS:
        endpoint = _model_endpoint(data, kind)
        api_base = str(endpoint.get("api_base") or "").rstrip("/")
        models[kind] = {
            "model": str(endpoint.get("model") or ""),
            "endpoint_id": by_url.get(api_base, ""),
        }
    return {
        "ok": True,
        "models": models,
        "config_path": MODEL_CONFIG_PATH,
        "endpoint_registry_path": MODEL_ENDPOINTS_PATH,
    }


def _validated_model_connection(kind: str, value, current: dict) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} 配置必须是对象")
    endpoint = str(value.get("endpoint") or "").strip().rstrip("/")
    api_key = str(value.get("api_key") or "").strip()
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
    return endpoint, api_key


def _validated_model_value(kind: str, value, current: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} 配置必须是对象")
    model = str(value.get("model") or "").strip()
    if not model or len(model) > 300 or any(ch in model for ch in "\r\n\t"):
        raise ValueError(f"{kind} 模型名不能为空，且不能包含换行")
    endpoint, api_key = _validated_model_connection(kind, value, current)
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


def _write_json_atomic(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mindmemos-endpoints.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _endpoint_registry() -> dict:
    try:
        with open(MODEL_ENDPOINTS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        config = _model_config()
        endpoints = []
        by_url = {}
        for kind in MODEL_ROUTERS:
            route = _model_endpoint(config, kind)
            url = str(route.get("api_base") or "").strip().rstrip("/")
            if not url:
                continue
            if url in by_url:
                continue
            item = {
                "id": "ep_" + hashlib.sha256(url.encode()).hexdigest()[:12],
                "name": urllib.parse.urlsplit(url).hostname or f"Endpoint {len(endpoints) + 1}",
                "endpoint": url,
                "api_key": str(route.get("api_key") or ""),
                "models": [],
                "fetched_at": None,
            }
            endpoints.append(item)
            by_url[url] = item
        data = {"version": 1, "endpoints": endpoints}
        _write_json_atomic(MODEL_ENDPOINTS_PATH, data)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Endpoint 注册表不是有效 JSON") from exc
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list) or any(not isinstance(item, dict) for item in endpoints):
        raise ValueError("Endpoint 注册表格式不正确")
    return data


def _public_endpoint_registry() -> dict:
    data = _endpoint_registry()
    endpoints = []
    catalog = []
    for item in data["endpoints"]:
        models = [str(value) for value in item.get("models", []) if str(value).strip()]
        endpoints.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "endpoint": str(item.get("endpoint") or ""),
                "key_configured": bool(str(item.get("api_key") or "").strip()),
                "model_count": len(models),
                "fetched_at": item.get("fetched_at"),
            }
        )
        catalog.extend(
            {
                "endpoint_id": str(item.get("id") or ""),
                "endpoint_name": str(item.get("name") or ""),
                "id": model_id,
            }
            for model_id in models
        )
    return {
        "ok": True,
        "endpoints": endpoints,
        "catalog": catalog,
        "registry_path": MODEL_ENDPOINTS_PATH,
    }


def _find_registered_endpoint(registry: dict, endpoint_id: str) -> dict:
    for item in registry["endpoints"]:
        if str(item.get("id") or "") == endpoint_id:
            return item
    raise ValueError("找不到指定 Endpoint")


def _save_registered_endpoint(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    with _MODEL_SETTINGS_LOCK:
        registry = _endpoint_registry()
        endpoint_id = str(payload.get("id") or "").strip()
        current = _find_registered_endpoint(registry, endpoint_id) if endpoint_id else {}
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 80 or any(ch in name for ch in "\r\n\t"):
            raise ValueError("Endpoint 名称不能为空且不能超过 80 字")
        endpoint, api_key = _validated_model_connection("Endpoint", payload, current)
        for item in registry["endpoints"]:
            if item is not current and str(item.get("endpoint") or "").rstrip("/") == endpoint:
                raise ValueError("这个 Endpoint 地址已经存在")
        models = sorted(_provider_model_ids(endpoint, api_key), key=str.casefold)
        if not endpoint_id:
            endpoint_id = "ep_" + hashlib.sha256((endpoint + name).encode()).hexdigest()[:12]
            current = {}
            registry["endpoints"].append(current)
        current.update(
            {
                "id": endpoint_id,
                "name": name,
                "endpoint": endpoint,
                "api_key": api_key,
                "models": models,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_json_atomic(MODEL_ENDPOINTS_PATH, registry)
    return _public_endpoint_registry()


def _refresh_registered_endpoints(payload: dict) -> dict:
    endpoint_id = str((payload or {}).get("id") or "").strip()
    with _MODEL_SETTINGS_LOCK:
        registry = _endpoint_registry()
        targets = [_find_registered_endpoint(registry, endpoint_id)] if endpoint_id else registry["endpoints"]
        results = []
        changed = False
        for item in targets:
            try:
                models = sorted(
                    _provider_model_ids(str(item.get("endpoint") or ""), str(item.get("api_key") or "")),
                    key=str.casefold,
                )
                item["models"] = models
                item["fetched_at"] = datetime.now(timezone.utc).isoformat()
                changed = True
                results.append({"id": item["id"], "ok": True, "model_count": len(models)})
            except ValueError as exc:
                results.append({"id": item.get("id"), "ok": False, "error": str(exc)})
        if changed:
            _write_json_atomic(MODEL_ENDPOINTS_PATH, registry)
    return {**_public_endpoint_registry(), "results": results}


def _delete_registered_endpoint(payload: dict) -> dict:
    endpoint_id = str((payload or {}).get("id") or "").strip()
    with _MODEL_SETTINGS_LOCK:
        registry = _endpoint_registry()
        target = _find_registered_endpoint(registry, endpoint_id)
        target_url = str(target.get("endpoint") or "").rstrip("/")
        config = _model_config()
        used_by = [
            kind
            for kind in MODEL_ROUTERS
            if str(_model_endpoint(config, kind).get("api_base") or "").rstrip("/") == target_url
        ]
        if used_by:
            raise ValueError("Endpoint 正被以下模型使用，不能删除：" + "、".join(used_by))
        registry["endpoints"] = [item for item in registry["endpoints"] if item is not target]
        _write_json_atomic(MODEL_ENDPOINTS_PATH, registry)
    return _public_endpoint_registry()


def _resolved_model_value(kind: str, value, current: dict, registry: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} 配置必须是对象")
    endpoint_id = str(value.get("endpoint_id") or "").strip()
    if not endpoint_id:
        return _validated_model_value(kind, value, current)
    endpoint = _find_registered_endpoint(registry, endpoint_id)
    model = str(value.get("model") or "").strip()
    model_id = str(value.get("model_id") or "").strip()
    if not model or not model_id or model_id not in endpoint.get("models", []):
        raise ValueError(f"{kind} 必须从缓存模型目录中选择")
    if model != model_id and not model.endswith("/" + model_id):
        raise ValueError(f"{kind} 模型值与缓存目录不匹配")
    return {
        "model": model,
        "api_base": str(endpoint.get("endpoint") or ""),
        "api_key": str(endpoint.get("api_key") or ""),
    }


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
        registry = _endpoint_registry()
        for kind in MODEL_ROUTERS:
            target = _model_endpoint(config, kind)
            target.update(_resolved_model_value(kind, models[kind], target, registry))

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
    escaped_key = api_key.replace("\\", "\\\\").replace('"', '\\"')
    curl_config = f'header = "Authorization: Bearer {escaped_key}"\nheader = "Accept: application/json"\n'
    fd, output_path = tempfile.mkstemp(prefix=".mindmemos-model-list.")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--ipv4",
                "--silent",
                "--show-error",
                "--max-time",
                "15",
                "--output",
                output_path,
                "--write-out",
                "%{http_code}",
                "--config",
                "-",
                url,
            ],
            input=curl_config,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            print(
                f"model endpoint curl failed: exit={result.returncode} stderr={result.stderr[-500:]!r}",
                file=sys.stderr,
            )
            raise ValueError("Endpoint 连接失败，请检查地址、网络和服务状态")
        try:
            status = int(result.stdout.strip())
        except ValueError as exc:
            raise ValueError("Endpoint 测试没有返回 HTTP 状态") from exc
        if status in (401, 403):
            raise ValueError("API Key 无效或无权读取模型列表")
        if status == 404:
            raise ValueError("Endpoint 没有 /models 接口，请确认它是 OpenAI 兼容地址")
        if status >= 400:
            raise ValueError(f"Endpoint 返回 HTTP {status}")
        if os.path.getsize(output_path) > 2_000_000:
            raise ValueError("Endpoint 返回的模型列表过大")
        with open(output_path, "rb") as handle:
            data = json.load(handle)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Endpoint 连接失败，请检查地址、网络和服务状态") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("API Key", "Endpoint")):
            raise
        raise ValueError("Endpoint 返回的不是有效模型列表") from exc
    finally:
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
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


def _probe_rerank(endpoint: str, api_key: str, model: str) -> None:
    escaped_key = api_key.replace("\\", "\\\\").replace('"', '\\"')
    curl_config = (
        f'header = "Authorization: Bearer {escaped_key}"\n'
        'header = "Content-Type: application/json"\n'
        'header = "Accept: application/json"\n'
    )
    payload = json.dumps(
        {
            "model": model,
            "query": "MindMemOS connection test",
            "documents": ["MindMemOS connection test"],
            "top_n": 1,
        },
        ensure_ascii=False,
    ).encode()
    body_fd, body_path = tempfile.mkstemp(prefix=".mindmemos-rerank-body.")
    output_fd, output_path = tempfile.mkstemp(prefix=".mindmemos-rerank-result.")
    try:
        with os.fdopen(body_fd, "wb") as body_file:
            body_file.write(payload)
        os.chmod(body_path, 0o600)
        os.close(output_fd)
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "--ipv4",
                "--silent",
                "--show-error",
                "--max-time",
                "20",
                "--request",
                "POST",
                "--data-binary",
                f"@{body_path}",
                "--output",
                output_path,
                "--write-out",
                "%{http_code}",
                "--config",
                "-",
                endpoint.rstrip("/") + "/rerank",
            ],
            input=curl_config,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        if result.returncode:
            raise ValueError("Rerank Endpoint 连接失败，请检查地址和网络")
        status = int(result.stdout.strip())
        if status in (401, 403):
            raise ValueError("Rerank API Key 无效或没有调用权限")
        if status == 404:
            raise ValueError("Endpoint 没有 /rerank 接口")
        if status >= 400:
            raise ValueError(f"Rerank Endpoint 返回 HTTP {status}")
        with open(output_path, "rb") as output_file:
            response = json.load(output_file)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ValueError("Rerank Endpoint 返回中没有 results")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError("Rerank Endpoint 测试失败，请检查服务状态") from exc
    finally:
        for path in (body_path, output_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _test_model_settings(payload: dict) -> dict:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict) or set(models) != set(MODEL_ROUTERS):
        raise ValueError("models 必须完整包含 llm、embedding、rerank")
    config = _model_config()
    registry = _endpoint_registry()
    prepared = {}
    for kind in MODEL_ROUTERS:
        current = _model_endpoint(config, kind)
        prepared[kind] = _resolved_model_value(kind, models[kind], current, registry)
    results = {}
    for kind, item in prepared.items():
        requested = item["model"]
        provider_model = requested.split("/", 1)[1] if "/" in requested else requested
        if kind == "rerank":
            try:
                _probe_rerank(item["api_base"], item["api_key"], provider_model)
                results[kind] = {
                    "ok": True,
                    "message": f"连接成功，模型 {provider_model} 已完成最小 Rerank 请求",
                }
            except ValueError as exc:
                results[kind] = {"ok": False, "message": str(exc)}
            continue
        results[kind] = {
            "ok": True,
            "message": f"已从 Endpoint 缓存目录确认模型 {provider_model}",
        }
    return {"ok": all(item["ok"] for item in results.values()), "results": results}


def _beijing_day(value) -> str:
    """把存储层 UTC 时间归到北京时间自然日；无法解析时返回空串。"""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # MindMemOS 历史数据里的无 offset 时间也是 UTC；明确补上，禁止按机器本地时区猜。
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _recent_snapshot(rows: list[dict], *, now: datetime | None = None, days: int = 30) -> dict:
    """Return one Beijing-time 30-day view with explicit zero-count calendar days."""
    reference = now or datetime.now(BEIJING_TZ)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=BEIJING_TZ)
    today = reference.astimezone(BEIJING_TZ).date()
    dated = [row for row in rows if row.get("created")]
    dated.sort(key=lambda row: str(row["created"]), reverse=True)
    counts = Counter(day for day in (_beijing_day(row["created"]) for row in dated) if day)
    by_day = {
        (today - timedelta(days=offset)).isoformat(): counts.get((today - timedelta(days=offset)).isoformat(), 0)
        for offset in range(max(1, days))
    }
    return {"today": today.isoformat(), "items": dated[:60], "by_day": by_day}


def _read_rules() -> list[str]:
    try:
        raw = open(PINNED_PATH, encoding="utf-8").read()
    except FileNotFoundError:
        return []
    return [block.strip() for block in raw.split("\n§\n") if len(block.strip()) >= 8]


def _write_rules(rules: list[str]) -> None:
    """Validate and atomically replace the pinned behavior rules."""
    cleaned = []
    for value in rules:
        rule = str(value).strip()
        if not rule:
            continue
        if len(rule) < 8:
            raise ValueError("每条准则至少 8 个字符")
        if len(rule) > 2000:
            raise ValueError("单条准则不要超过 2000 个字符")
        if "\n§\n" in rule:
            raise ValueError("准则内容不能包含分隔符 §")
        cleaned.append(rule)
    if len(cleaned) > 80:
        raise ValueError("行为准则最多 80 条")
    os.makedirs(os.path.dirname(PINNED_PATH), exist_ok=True)
    if os.path.exists(PINNED_PATH):
        shutil.copy2(PINNED_PATH, PINNED_PATH + ".previous")
    fd, tmp = tempfile.mkstemp(prefix=".mindmemos_pinned.", dir=os.path.dirname(PINNED_PATH), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n§\n".join(cleaned) + ("\n" if cleaned else ""))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, PINNED_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _bump_data_version() -> None:
    os.makedirs(os.path.dirname(PANEL_VERSION_PATH), exist_ok=True)
    with open(PANEL_VERSION_PATH, "w", encoding="ascii") as handle:
        handle.write(str(datetime.now(timezone.utc).timestamp()))
    os.chmod(PANEL_VERSION_PATH, 0o600)


def _data_version() -> dict:
    """Return revisions for successful memory commits and panel mutations only."""
    memory_revision = 0.0
    ledger_path = getattr(provenance_ledger, "path", LEDGER_PATH) if provenance_ledger else LEDGER_PATH
    try:
        with sqlite3.connect(ledger_path) as connection:
            row = connection.execute("SELECT COALESCE(MAX(updated_at), 0) FROM memory_lineage").fetchone()
            memory_revision = float(row[0] or 0)
    except (OSError, sqlite3.Error):
        pass
    try:
        panel_revision = os.stat(PANEL_VERSION_PATH).st_mtime_ns
    except OSError:
        panel_revision = 0
    return {
        "memory_revision": memory_revision,
        "panel_revision": panel_revision,
        "version": f"{memory_revision:.6f}:{panel_revision}",
    }


def _lan_ip() -> str:
    """本机内网 IP，用于告诉另一台机器该连哪里。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))  # 不实际发包，只为拿到出口网卡地址
        return s.getsockname()[0]
    except Exception:
        return "192.168.1.246"
    finally:
        s.close()


LAN_IP = os.getenv("MINDMEMOS_LAN_IP") or _lan_ip()
LLMS_URL = os.getenv("MM_LLMS_URL", f"http://{LAN_IP}:8765/llms.txt")


# ---- token 与 provenance：共用 MindMemOS 实现，不在面板复制逻辑 ----
import importlib.util as _ilu  # noqa: E402

_MM_ROOT = os.path.expanduser(os.getenv("MINDMEMOS_ROOT", "~/Projects/MindMemOS"))


def _load_module(name: str, path: str):
    spec = _ilu.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = _ilu.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


try:
    mcp_tokens = _load_module("mm_panel_tokens", os.path.join(_MM_ROOT, "mcp_tokens.py"))
except Exception as _e:  # 面板不该因为这个起不来
    mcp_tokens = None
    print(f"⚠️  token 模块加载失败（{_e}），令牌管理页不可用", file=sys.stderr)

try:
    turn_ingest = _load_module("mm_panel_turn_ingest", os.path.join(_MM_ROOT, "turn_ingest.py"))
    provenance_ledger = turn_ingest.TurnLedger()
except Exception as _e:
    turn_ingest = None
    provenance_ledger = None
    print(f"⚠️  provenance 模块加载失败（{_e}），来源标签不可用", file=sys.stderr)

_PANEL_INSTANCE = (os.getenv("MM_PANEL_INSTANCE") or socket.gethostname().split(".")[0]).lower()
PANEL_IMPORT_PRINCIPAL = {
    "client_id": f"mm-panel-{_PANEL_INSTANCE}",
    "agent_kind": "operator",
    "instance": _PANEL_INSTANCE,
    "credential_id": "local-panel",
    "display_name": "Panel import",
    "scope": "write",
    "authority": "local_panel",
}


def _is_lan(addr: str) -> bool:
    """Allow direct localhost/RFC1918/ULA callers; never trust forwarding headers."""

    if addr == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    return any(ip in network for network in networks)


def _install_script() -> str:
    """Generate a client-neutral preflight for one Agent instance."""

    return f"""#!/usr/bin/env bash
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
"""


# whoami 结果缓存：(写入时间, 响应体)。5 次检索要几秒，没必要每次点都重算
_WHO_CACHE = None

_EXTRACTOR = None


def _parse_multipart(body: bytes, content_type: str):
    """解析 multipart/form-data，返回 (文件列表, 普通字段)。

    为什么手写：Python 3.13 起标准库移除了 cgi 模块（PEP 594），
    而面板跑在 3.14 上。只需要文件+文本字段这点功能，不值得引依赖。
    """
    m = re.search(r'boundary=("?)([^";]+)\1', content_type or "")
    if not m:
        raise ValueError("缺少 multipart boundary")
    sep = b"--" + m.group(2).encode()
    files, fields = [], {}
    for part in body.split(sep):
        if not part.strip() or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        h = head.decode("utf-8", "ignore")
        nm = re.search(r'name="([^"]*)"', h)
        fn = re.search(r'filename="([^"]*)"', h)
        if not nm:
            continue
        if fn and fn.group(1):
            files.append((fn.group(1), data))
        else:
            fields[nm.group(1)] = data.decode("utf-8", "ignore").strip()
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

    mm = _MM_ROOT
    venv = os.path.expanduser(os.getenv("MINDMEMOS_VENV", os.path.join(mm, ".venv")))
    for sp in glob.glob(os.path.join(venv, "lib/python*/site-packages")):
        if sp not in sys.path:
            sys.path.append(sp)
    path = os.path.join(mm, "scripts/ingest/extractor.py")
    spec = importlib.util.spec_from_file_location("mm_extractor", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"找不到 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _EXTRACTOR = mod
    return mod


# 实体来源有两路：LLM（vanilla_llm）和 spaCy NER。
# LLM 用下面这套干净类型；spaCy 产出 cardinal/date/term/norp 等噪音
# （"06"、"1"、"2026" 这种纯数字度数还最高），默认只画 LLM 类型。
LLM_TYPES = {"person", "organization", "location", "project", "product", "tool", "file", "model", "version", "other"}
NOISY_TYPES = {
    "cardinal",
    "date",
    "ordinal",
    "time",
    "percent",
    "money",
    "quantity",
    "term",
    "technical_term",
    "norp",
    "work",
    "language",
    "code",
    "proper_noun",
    "acronym",
    "quoted_text",
}


def cypher(query, params=None):
    """跑一条 Cypher，返回 [{列名: 值}]。"""
    import base64

    body = {"statements": [{"statement": query, "parameters": params or {}}]}
    token = base64.b64encode(f"{NEO4J_AUTH[0]}:{NEO4J_AUTH[1]}".encode()).decode()
    req = urllib.request.Request(
        NEO4J,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Basic {token}"},
    )
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
        body = {"limit": min(512, limit - len(out)), "with_payload": with_payload, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        r = http_json(f"{QDRANT}/collections/{coll}/points/scroll", body)["result"]
        pts = r.get("points", [])
        out.extend(pts)
        offset = r.get("next_page_offset")
        if not offset or not pts:
            break
    return out


def _attach_provenance(rows):
    memory_ids = [str(row.get("id") or row.get("memory_id") or "") for row in rows]
    found = provenance_ledger.provenance_for(memory_ids) if provenance_ledger else {}
    for row, memory_id in zip(rows, memory_ids):
        provenance = found.get(memory_id)
        if provenance is None and row.get("app_id"):
            agent_kind, _, instance = str(row.get("agent_id") or "legacy:unknown").partition(":")
            contributor = {
                "client_id": row["app_id"],
                "agent_kind": agent_kind or "legacy",
                "instance": instance or "unknown",
                "display_name": instance or row["app_id"],
                "authority": "historical_payload",
                "last_capture_mode": "unknown",
                "capture_modes": ["unknown"],
            }
            provenance = {
                "origin": {**contributor, "capture_mode": "unknown"},
                "last_source": {**contributor, "capture_mode": "unknown"},
                "contributors": [contributor],
            }
        row["provenance"] = provenance
    return rows


SOURCE_CHANNEL_LABELS = {
    "feishu_wiki": "飞书文档",
    "hermes_turn": "Hermes 对话",
    "hermes_daily": "Hermes 日常记忆",
    "project_memory": "项目记忆导入",
    "hermes_builtin_memory": "Hermes 内置记忆",
    "mcp": "MCP 显式写入",
    "hermes_explicit_correction": "Hermes 显式纠正",
    "hermes-migrate": "Hermes 迁移",
}
SOURCE_ROLE_LABELS = {"user": "用户消息", "assistant": "助手消息", "system": "系统消息"}
TOPIC_TYPE_PRIORITY = {
    "project": 0,
    "product": 1,
    "tool": 2,
    "organization": 3,
    "person": 4,
    "model": 5,
    "location": 6,
    "file": 7,
    "other": 8,
}


def _source_info(metadata):
    """Build one truthful UI source label from persisted provenance fields."""

    metadata = metadata or {}
    source_id = str(metadata.get("source_id") or "")
    source_type = str(metadata.get("source_type") or "message")
    role = str(metadata.get("source_role") or metadata.get("source_raw_role") or "")
    channel = str(metadata.get("source") or "")
    doc = str(metadata.get("doc") or metadata.get("file_name") or "").strip()
    if doc:
        label = doc
    elif channel in SOURCE_CHANNEL_LABELS:
        label = SOURCE_CHANNEL_LABELS[channel]
    elif source_type == "file":
        label = "文件"
    elif source_type == "url":
        label = "网页"
    elif source_type == "memory":
        label = "历史记忆重提炼"
    else:
        label = "对话"
    role_label = SOURCE_ROLE_LABELS.get(role)
    if source_type == "message" and role_label and role_label not in label:
        label = f"{label} · {role_label}"
    return {
        "id": source_id,
        "type": source_type,
        "role": role,
        "message_index": metadata.get("source_message_index"),
        "channel": channel,
        "label": label,
    }


def _attach_topics(rows):
    """Attach ranked topic-like entities to each memory for card display."""

    by_id = {str(row.get("id") or ""): row for row in rows if row.get("id")}
    for row in rows:
        fallback = []
        for name in row.get("entities") or []:
            text = str(name or "").strip()
            if text and not text.isdigit() and text not in {item["name"] for item in fallback}:
                fallback.append({"name": text, "type": "entity"})
        row["topics"] = fallback[:4]
        row["topic"] = fallback[0]["name"] if fallback else "未归类"
    if not by_id:
        return rows
    try:
        graph_rows = cypher(
            """
            MATCH (m:Memory)-[:MENTIONS]->(e:Entity)
            WHERE m.memory_id IN $memory_ids
            RETURN m.memory_id AS memory_id,
                   e.entity_name AS entity_name,
                   e.entity_type AS entity_type
            """,
            {"memory_ids": list(by_id)},
        )
    except Exception:
        return rows
    grouped = defaultdict(list)
    for item in graph_rows:
        memory_id = str(item.get("memory_id") or "")
        name = str(item.get("entity_name") or "").strip()
        entity_type = str(item.get("entity_type") or "other").lower()
        if memory_id not in by_id or not name or name.isdigit() or entity_type in NOISY_TYPES:
            continue
        if name not in {topic["name"] for topic in grouped[memory_id]}:
            grouped[memory_id].append({"name": name, "type": entity_type})
    for memory_id, topics in grouped.items():
        topics.sort(key=lambda item: (TOPIC_TYPE_PRIORITY.get(item["type"], 99), item["name"].lower()))
        by_id[memory_id]["topics"] = topics[:4]
        by_id[memory_id]["topic"] = topics[0]["name"]
    return rows


def clean_memories(points):
    """只保留真正的记忆条目（有 content 的），并标注噪音。"""
    rows = []
    for p in points:
        pl = p.get("payload", {})
        if str(pl.get("status") or "active") != "active":
            continue
        c = pl.get("content")
        if not c:
            continue
        md = pl.get("metadata", {}) or {}
        source = _source_info(md)
        doc = source["label"]
        # 噪音判定：内容主要在描述文档路径/分片位置本身，而非真实知识
        noise = (
            ("长期档案" in c and ("路径" in c or "文档" in c or "/" in c))
            or ("部分" in c and ("第" in c[:40] or "/" in c[:40]))
            or c.strip().startswith("以下是")
        )
        rows.append(
            {
                "id": pl.get("memory_id"),
                "content": c,
                "type": pl.get("mem_type") or "unknown",
                "doc": doc,
                "source": source,
                # 保留完整 ISO 时区；前端统一格式化为 Asia/Shanghai。
                "created": str(pl.get("created_at") or ""),
                "entities": (md.get("entities") or [])[:6],
                "noise": bool(noise),
                "app_id": pl.get("app_id"),
                "agent_id": pl.get("agent_id"),
            }
        )
    _attach_provenance(rows)
    _attach_topics(rows)
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows


def _load_recall_judge_endpoint() -> JudgeEndpoint:
    registry = _endpoint_registry()
    endpoint = _find_registered_endpoint(registry, RECALL_JUDGE_ENDPOINT_ID)
    models = {str(value) for value in endpoint.get("models", [])}
    if RECALL_JUDGE_MODEL not in models:
        raise ValueError(f"Judge 模型未出现在 Endpoint 目录：{RECALL_JUDGE_MODEL}")
    api_key = str(endpoint.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Judge Endpoint 尚未配置凭据")
    return JudgeEndpoint(
        url=str(endpoint.get("endpoint") or "").strip(),
        api_key=api_key,
        model=RECALL_JUDGE_MODEL,
    )


def _judge_snapshot(points: list[dict], store: RecallReviewStore) -> dict:
    return build_recall_snapshot(points, store, limit=RECALL_JUDGE_RECENT_CALLS)


def _judge_points() -> list[dict]:
    return scroll_all(SEARCH_COLL, limit=10_000)


RECALL_JUDGE = RecallJudge(
    store=RECALL_REVIEWS,
    endpoint_loader=_load_recall_judge_endpoint,
    point_loader=_judge_points,
    normalizer=_judge_snapshot,
    recent_calls=RECALL_JUDGE_RECENT_CALLS,
    interval_seconds=RECALL_JUDGE_INTERVAL_SECONDS,
    enabled=RECALL_JUDGE_ENABLED,
)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/dashboard.js":
            b = open(os.path.join(HERE, "dashboard.js"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/model-registry.js":
            b = open(os.path.join(HERE, "model-registry.js"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/lucide.js":
            # 本地托管，不依赖外网 CDN（内网机器可能上不了外网）
            try:
                b = open(os.path.join(HERE, "lucide.js"), "rb").read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path == "/bootstrap.json":
            # 只对内网直连返回凭据；经 223 反代进来的外网请求一律 403。
            # 外网机器接入的正确路径是：人工在面板生成 token → 手动填进客户端。
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "bootstrap 只在内网可用；请在面板「访问令牌」页生成 token"}, 403)
                return
            try:
                cfg = json.load(open(CLIENT_CONFIG_PATH, encoding="utf-8"))
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return
            try:
                mcp_token = open(LEGACY_TOKEN_PATH, encoding="utf-8").read().strip()
            except Exception:
                mcp_token = ""
            self._send(
                {
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
                }
            )
            return
        if path in ("/migrate.py", "/mcp_server.py", "/ingest.py"):
            # 让另一台机器能直接 curl 下载，不用 scp
            src = {
                "/migrate.py": os.path.expanduser("~/Projects/MindMemOS/migrate_hermes_to_mm.py"),
                "/mcp_server.py": os.path.expanduser("~/Projects/MindMemOS/mcp_server.py"),
                "/ingest.py": os.path.expanduser("~/Projects/MindMemOS/scripts/ingest/cli.py"),
            }[path]
            try:
                b = open(src, "rb").read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if path.startswith("/skills/"):
            # 对外分发 skill，跟 llms.txt 同级。别的机器不用 clone 仓库。
            name = os.path.basename(path[len("/skills/") :])
            if not name.endswith(".md") or "/" in name or ".." in name:
                self.send_error(404)
                return
            try:
                b = open(os.path.join(HERE, "skills", name), "rb").read()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if path == "/install.sh":
            # 内嵌 token，因此跟 bootstrap.json 同级：只在内网发。
            if not _is_lan(self.client_address[0]):
                self.send_error(403, "install.sh is LAN-only")
                return
            b = _install_script().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if path == "/api/tokens":
            # 列出已签发的 token（只有元数据，没有明文也没有 hash）
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            if mcp_tokens is None:
                self._send({"ok": False, "error": "token 模块未加载"}, 500)
                return
            self._send({"ok": True, "tokens": mcp_tokens.listing(), "mcp_url": f"http://{LAN_IP}:8765/mcp"})
            return

        if path == "/api/model-endpoints":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                self._send(_public_endpoint_registry())
            except ValueError as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/models":
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

        if path == "/llms.txt":
            # 8765 的仓库根 llms.txt 是唯一真源；8666 不再维护第二份副本。
            self.send_response(302)
            self.send_header("Location", LLMS_URL)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/suggest":
            # 建议问题动态生成：从图谱取高频实体 + 最近活跃实体，
            # 拼成自然问句。写死的示例会随着项目演进过期。
            import random

            try:
                hot = cypher(
                    "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                    "WHERE e.entity_type IN ['project','tool','product'] "
                    "AND size(e.entity_name) < 24 "
                    "RETURN e.entity_name AS n, count(m) AS c "
                    "ORDER BY c DESC LIMIT 24"
                )
                fresh = cypher(
                    "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                    "WHERE e.entity_type IN ['project','tool'] "
                    "AND size(e.entity_name) < 24 AND m.created_at IS NOT NULL "
                    "RETURN e.entity_name AS n, max(m.created_at) AS last "
                    "ORDER BY last DESC LIMIT 12"
                )
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return

            # 问句模板：覆盖"是什么/限制/决策/踩坑"几类真实会问的角度
            tpl = [
                "{n} 是什么",
                "{n} 有什么已知限制",
                "{n} 的架构是怎样的",
                "{n} 有什么约束和铁律",
                "{n} 踩过什么坑",
                "{n} 用了哪些技术",
            ]
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
            self._send({"ok": True, "suggestions": picked, "pool": len(set(names_hot) | set(names_new))})
            return
        if path == "/api/whoami":
            # 用户画像：把散在各处的身份信息按维度聚合。
            # 跟 MCP 的 whoami 工具同一套逻辑，面板上也能直接看。
            # 走 5 次检索要几秒，加个 10 分钟缓存（?fresh=1 强制刷新）
            import time as _t

            global _WHO_CACHE
            fresh = "fresh=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
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
                    d = http_json(
                        f"{MM_API}/v1/memory/search",
                        {
                            "user_id": USER_ID,
                            "query": q,
                            "top_k": 4,
                            # 身份类查询开 rerank 反而把技术记忆排前面
                            "rerank": False,
                            "score_threshold": 0.05,
                        },
                        headers={"Authorization": f"Bearer {MM_KEY}"},
                    )
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
        if path == "/api/recall-evaluations":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                points = scroll_all(SEARCH_COLL, limit=10_000)
                snapshot = build_recall_snapshot(points, RECALL_REVIEWS, limit=limit)
                snapshot["ai_judge"] = {
                    "enabled": RECALL_JUDGE.enabled,
                    "model": RECALL_JUDGE_MODEL,
                    "recent_calls": RECALL_JUDGE_RECENT_CALLS,
                    "interval_seconds": RECALL_JUDGE_INTERVAL_SECONDS,
                    "last_run": RECALL_REVIEWS.ai_status(),
                }
                self._send(snapshot)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}, 500)
            return
        if path == "/api/all":
            snapshot_version = _data_version()
            try:
                pts = scroll_all(COLL)
                rows = clean_memories(pts)
                ents = http_json(f"{QDRANT}/collections/{ENT_COLL}", None)["result"]
                stats = {
                    "memories": len(rows),
                    "noise": sum(1 for r in rows if r["noise"]),
                    "entities": ents.get("points_count", 0),
                    "by_type": dict(Counter(r["type"] for r in rows).most_common()),
                    "by_doc": dict(Counter(r["doc"] for r in rows).most_common()),
                }
                self._send(
                    {"ok": True, "rows": rows, "stats": stats, "recent": _recent_snapshot(rows), **snapshot_version}
                )
            except Exception as e:
                self._send({"ok": False, "error": str(e)[:300]}, 500)
            return
        if path == "/api/health":
            out = {}
            for name, url in [
                ("mindmemos", f"{MM_API}/healthz"),
                ("qdrant", f"{QDRANT}/healthz"),
                ("neo4j", "http://127.0.0.1:7474"),
            ]:
                try:
                    urllib.request.urlopen(url, timeout=3)
                    out[name] = True
                except Exception:
                    out[name] = False
            self._send(out)
            return
        if path == "/api/quality-alerts":
            try:
                points = scroll_all(ADD_COLL, limit=2000)
                failures = []
                for point in points:
                    payload = point.get("payload") or {}
                    if payload.get("status") != "error" or payload.get("retry_resolved_at"):
                        continue
                    failure = payload.get("failure") or {}
                    failures.append(
                        {
                            "add_record_id": str(point.get("id") or ""),
                            "error_code": failure.get("error_code") or "add_failed",
                            "error_stage": failure.get("error_stage"),
                            "chunk_index": failure.get("chunk_index"),
                            "boundary": failure.get("boundary"),
                            "attempts": failure.get("attempts"),
                            "retryable": bool(failure.get("retryable")),
                            "completed_at": payload.get("task_completed_at"),
                            "error": str(payload.get("error") or "")[:300],
                        }
                    )
                failures.sort(key=lambda item: str(item.get("completed_at") or ""), reverse=True)
                self._send(
                    {
                        "ok": True,
                        "count": len(failures),
                        "retryable": sum(1 for item in failures if item["retryable"]),
                        "items": failures[:50],
                    }
                )
            except Exception as exc:
                self._send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}, 500)
            return
        if path == "/api/version":
            # Cheap local revision check; no Qdrant scan and no queue-state mtime noise.
            self._send({"ok": True, **_data_version()})
            return
        if self.path.startswith("/api/graph"):
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
        focus = (q.get("focus", [""])[0] or "").strip().lower()
        limit = min(int(q.get("limit", ["60"])[0] or 60), 200)
        noisy = list(NOISY_TYPES) if q.get("noisy", ["0"])[0] != "1" else ["\x00"]

        try:
            if focus:
                # 以某实体为中心，抓它相关的记忆及这些记忆提到的其它实体
                rows = cypher(
                    """
                    MATCH (c:Entity) WHERE toLower(c.entity_name) CONTAINS $focus
                    WITH c LIMIT 3
                    MATCH (c)<-[:MENTIONS]-(m:Memory)-[:MENTIONS]->(e:Entity)
                    WHERE NOT e.entity_type IN $noisy
                    RETURN c.entity_name AS src, c.entity_type AS stype,
                           e.entity_name AS dst, e.entity_type AS dtype,
                           count(DISTINCT m) AS w
                    ORDER BY w DESC LIMIT $lim
                """,
                    {"focus": focus, "noisy": noisy, "lim": limit * 3},
                )
            else:
                # 全局主干：取共现最强的实体对
                rows = cypher(
                    """
                    MATCH (a:Entity)<-[:MENTIONS]-(m:Memory)-[:MENTIONS]->(b:Entity)
                    WHERE NOT a.entity_type IN $noisy AND NOT b.entity_type IN $noisy
                      AND a.entity_name < b.entity_name
                    WITH a, b, count(DISTINCT m) AS w
                    WHERE w >= 2
                    RETURN a.entity_name AS src, a.entity_type AS stype,
                           b.entity_name AS dst, b.entity_type AS dtype, w
                    ORDER BY w DESC LIMIT $lim
                """,
                    {"noisy": noisy, "lim": limit * 3},
                )
        except Exception as e:
            self._send({"ok": False, "error": str(e)[:300]}, 502)
            return

        nodes, links = {}, []
        for r in rows:
            for nm, tp in ((r["src"], r["stype"]), (r["dst"], r["dtype"])):
                if nm not in nodes:
                    nodes[nm] = {"id": nm, "type": tp or "term", "deg": 0}
                nodes[nm]["deg"] += r["w"]
            links.append({"source": r["src"], "target": r["dst"], "w": r["w"]})

        self._send(
            {
                "ok": True,
                "nodes": list(nodes.values()),
                "links": links,
                "types": sorted({n["type"] for n in nodes.values()}),
                "focus": focus,
            }
        )

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/recall-evaluations/judge":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            # Manual trigger is observation-only and returns immediately.
            threading.Thread(target=RECALL_JUDGE.run_once, name="recall-ai-judge-manual", daemon=True).start()
            self._send({"ok": True, "started": True})
            return

        if path == "/api/recall-evaluations/review":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 50_000:
                    raise ValueError("请求体大小不正确")
                body = json.loads(self.rfile.read(n))
                RECALL_REVIEWS.save(body)
                _bump_data_version()
                self._send({"ok": True})
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}, 500)
            return

        if path in ("/api/model-endpoints/save", "/api/model-endpoints/refresh", "/api/model-endpoints/delete"):
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > 20_000:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(n) or b"{}")
                if path.endswith("/save"):
                    result = _save_registered_endpoint(body)
                elif path.endswith("/refresh"):
                    result = _refresh_registered_endpoints(body)
                else:
                    result = _delete_registered_endpoint(body)
                self._send(result)
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/models/test":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 100_000:
                    raise ValueError("请求体为空或过大")
                body = json.loads(self.rfile.read(n))
                self._send(_test_model_settings(body))
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/models":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 100_000:
                    raise ValueError("请求体为空或过大")
                body = json.loads(self.rfile.read(n))
                self._send(_save_model_settings(body))
            except (ValueError, json.JSONDecodeError) as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/rules":
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 200_000:
                    raise ValueError("请求体为空或过大")
                body = json.loads(self.rfile.read(n))
                rules = body.get("rules")
                if not isinstance(rules, list):
                    raise ValueError("rules 必须是数组")
                _write_rules(rules)
                _bump_data_version()
                global _WHO_CACHE
                _WHO_CACHE = None
                self._send({"ok": True, "rules": _read_rules(), **_data_version()})
            except ValueError as e:
                self._send({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path in ("/api/tokens/issue", "/api/tokens/revoke"):
            # 签发 / 撤销 MCP token。只允许内网直连——
            # 外网经 223 反代进来的请求源地址不在白名单，天然被挡。
            if not _is_lan(self.client_address[0]):
                self._send({"ok": False, "error": "LAN only"}, 403)
                return
            if mcp_tokens is None:
                self._send({"ok": False, "error": "token 模块未加载"}, 500)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send({"ok": False, "error": f"请求体解析失败: {e}"}, 400)
                return
            try:
                if path.endswith("/issue"):
                    ttl = body.get("ttl_days")
                    rec = mcp_tokens.issue(
                        body.get("name") or "unnamed",
                        body.get("scope") or "read",
                        int(ttl) if ttl else None,
                        client_id=body.get("client_id") or None,
                        agent_kind=body.get("agent_kind") or None,
                        instance=body.get("instance") or None,
                        display_name=body.get("display_name") or None,
                    )
                    # token 明文只在这里出现一次，之后库里只有 sha256
                    self._send({"ok": True, "token": rec, "mcp_url": f"http://{LAN_IP}:8765/mcp"})
                else:
                    ok = mcp_tokens.revoke(body.get("id") or "")
                    self._send({"ok": ok, "error": None if ok else "找不到该 token 或已撤销"}, 200 if ok else 404)
            except Exception as e:
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if path == "/api/upload":
            # 界面上传文档。解析复用 MindMemOS/scripts/ingest/extractor.py，
            # 跟 CLI 是同一份实现，不会两边行为不一致。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    raise ValueError("空请求体")
                if n > 200 * 1024 * 1024:
                    raise ValueError("单次上传不要超过 200MB")
                body = self.rfile.read(n)
                items, fields = _parse_multipart(body, self.headers.get("Content-Type", ""))
            except Exception as e:
                self._send({"ok": False, "error": f"表单解析失败：{e}"}, 400)
                return

            tag = (fields.get("tag") or "").strip()
            if not items:
                self._send({"ok": False, "error": "没有收到文件"}, 400)
                return

            try:
                ext_mod = _load_extractor()
            except Exception as e:
                self._send({"ok": False, "error": f"抽取模块加载失败：{e}"}, 500)
                return

            results = []
            for fname, raw in items:
                name = os.path.basename(fname or "")
                if not name:
                    continue
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in ext_mod.EXTS:
                    results.append({"file": name, "ok": False, "detail": f"不支持的格式 {suffix}"})
                    continue
                tmp = ""
                try:
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                        tf.write(raw)
                        tmp = tf.name
                    text = ext_mod.extract(tmp)
                    cs = ext_mod.chunks(text)
                except Exception as e:
                    results.append({"file": name, "ok": False, "detail": f"抽取失败：{type(e).__name__}"})
                    continue
                finally:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)

                if not cs:
                    results.append({"file": name, "ok": False, "detail": "内容太少，已跳过"})
                    continue

                label = f"[文档：{name}]" + (f"[{tag}]" if tag else "")
                file_hash = hashlib.sha256(raw).hexdigest()
                good = 0
                failed_chunks = []
                for chunk_index, ch in enumerate(cs):
                    try:
                        event_id = (
                            "import-" + hashlib.sha256(f"{name}\0{tag}\0{chunk_index}\0{ch}".encode()).hexdigest()
                        )
                        d = http_json(
                            f"{MM_API}/v1/memory/add",
                            {
                                "user_id": USER_ID,
                                "app_id": PANEL_IMPORT_PRINCIPAL["client_id"],
                                "agent_id": (
                                    f"{PANEL_IMPORT_PRINCIPAL['agent_kind']}:{PANEL_IMPORT_PRINCIPAL['instance']}"
                                ),
                                "session_id": f"upload-{hashlib.sha256(name.encode()).hexdigest()[:12]}",
                                "messages": [{"role": "user", "content": f"{label}\n{ch}"}],
                                "sources": [
                                    {
                                        "source_type": "file",
                                        "file_path": f"upload://sha256/{file_hash}",
                                        "file_name": name,
                                        "is_parsed": True,
                                        "content_hash": file_hash,
                                        "chunk_id": f"{file_hash}:{chunk_index}",
                                        "start_offset": chunk_index,
                                        "metadata": {
                                            "message_index": 0,
                                            "tag": tag,
                                            "chunk_index": chunk_index,
                                            "chunk_count": len(cs),
                                            "capture_mode": "import",
                                        },
                                    }
                                ],
                                "mode": "sync",
                                "metadata": {
                                    "provenance": {
                                        **PANEL_IMPORT_PRINCIPAL,
                                        "capture_mode": "import",
                                        "event_id": event_id,
                                    },
                                    "document": {
                                        "name": name,
                                        "tag": tag,
                                        "chunk_index": chunk_index,
                                        "chunk_count": len(cs),
                                    },
                                },
                            },
                            headers={"Authorization": f"Bearer {MM_KEY}"},
                        )
                        response_ok = str(d.get("code", "")).lower() in ("ok", "0")
                        memories = (d.get("data") or {}).get("memories") or []
                        if response_ok and memories:
                            if provenance_ledger:
                                provenance_ledger.record_response(
                                    d,
                                    PANEL_IMPORT_PRINCIPAL,
                                    capture_mode="import",
                                    event_id=event_id,
                                )
                            good += 1
                        else:
                            failed_chunks.append(
                                {
                                    "chunk_index": chunk_index,
                                    "code": d.get("code") or "empty_extraction",
                                    "detail": d.get("message") or "该分块未提取出任何可持久化记忆",
                                }
                            )
                    except Exception as exc:
                        failed_chunks.append(
                            {
                                "chunk_index": chunk_index,
                                "code": type(exc).__name__,
                                "detail": str(exc)[:300],
                            }
                        )
                results.append(
                    {
                        "file": name,
                        "ok": good == len(cs),
                        "detail": f"{len(text)} 字符 → 入库 {good}/{len(cs)} 片",
                        "successful_chunks": good,
                        "total_chunks": len(cs),
                        "failed_chunks": failed_chunks,
                    }
                )
            if any(result.get("successful_chunks", 0) for result in results):
                _bump_data_version()
            self._send({"ok": bool(results) and all(result.get("ok") for result in results), "results": results})
            return
        if path == "/api/quality-retry":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                self._send({"ok": False, "error": f"bad json: {exc}"}, 400)
                return
            record_id = str(req.get("add_record_id") or "").strip()
            if not record_id:
                self._send({"ok": False, "error": "缺少 add_record_id"}, 400)
                return
            try:
                retrieved = http_json(
                    f"{QDRANT}/collections/{ADD_COLL}/points",
                    {"ids": [record_id], "with_payload": True, "with_vector": False},
                )
                points = (retrieved.get("result") or {}).get("points") or []
                if not points:
                    self._send({"ok": False, "error": "失败记录不存在"}, 404)
                    return
                original = points[0].get("payload") or {}
                messages = original.get("messages") or []
                sources = original.get("sources") or []
                if not messages:
                    self._send({"ok": False, "error": "失败记录没有可重试的原始输入"}, 409)
                    return
                metadata = dict(original.get("metadata") or {})
                metadata["retry_of_add_record_id"] = record_id
                d = http_json(
                    f"{MM_API}/v1/memory/add",
                    {
                        "user_id": original.get("user_id") or USER_ID,
                        "app_id": original.get("app_id"),
                        "agent_id": original.get("agent_id"),
                        "session_id": original.get("session_id"),
                        "messages": messages,
                        "sources": sources,
                        "mode": "sync",
                        "metadata": metadata,
                    },
                    headers={"Authorization": f"Bearer {MM_KEY}"},
                )
                ok = str(d.get("code", "")).lower() in ("ok", "0") and bool((d.get("data") or {}).get("memories"))
                if ok:
                    http_json(
                        f"{QDRANT}/collections/{ADD_COLL}/points/payload",
                        {
                            "payload": {
                                "retry_resolved_at": datetime.now(timezone.utc).isoformat(),
                                "retry_request_id": d.get("request_id"),
                            },
                            "points": [record_id],
                        },
                    )
                    _bump_data_version()
                self._send(
                    {"ok": ok, "detail": d.get("message") or d.get("code"), "request_id": d.get("request_id")},
                    200 if ok else 502,
                )
            except Exception as exc:
                self._send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, 500)
            return
        if path in ("/api/delete", "/api/update"):
            # 编辑/删除走 MM 官方接口，不直接动 Qdrant——
            # 直接删向量库会漏掉 Neo4j 里的实体和边。
            # delete 是**软删除**（status 置 archived），数据仍在但不再被召回。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send({"ok": False, "error": f"bad json: {e}"}, 400)
                return
            mid = (req.get("memory_id") or "").strip()
            if not mid:
                self._send({"ok": False, "error": "缺少 memory_id"}, 400)
                return
            body = {"memory_id": mid}
            if path == "/api/update":
                c = (req.get("content") or "").strip()
                if not c:
                    self._send({"ok": False, "error": "内容不能为空"}, 400)
                    return
                body["content"] = c
            ep = "/v1/memory/delete" if path == "/api/delete" else "/v1/memory/update"
            try:
                d = http_json(f"{MM_API}{ep}", body, headers={"Authorization": f"Bearer {MM_KEY}"})
            except Exception as e:
                self._send({"ok": False, "error": str(e)}, 500)
                return
            ok = str(d.get("code", "")).lower() in ("ok", "0", "success")
            if ok:
                _bump_data_version()
            self._send({"ok": ok, "detail": d.get("message") or d.get("code")})
            return
        if path != "/api/search":
            self._send({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        payload = {
            "user_id": req.get("user_id") or USER_ID,
            "query": req.get("query", ""),
            "top_k": int(req.get("top_k") or 10),
            "rerank": True,
            "score_threshold": float(req.get("score_threshold", 0.1)),
        }
        try:
            r = http_json(f"{MM_API}/v1/memory/search", payload, {"Authorization": f"Bearer {MM_KEY}"})
            mems = r.get("data", {}).get("memories", []) or []
            _attach_provenance(mems)
            self._send({"ok": True, "memories": mems})
        except urllib.error.HTTPError as e:
            self._send({"ok": False, "error": f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}"}, 502)
        except Exception as e:
            self._send({"ok": False, "error": str(e)[:300]}, 502)


if __name__ == "__main__":
    print(f"MindMemOS 面板  ->  http://{HOST}:{PORT}")
    RECALL_REVIEWS.mark_interrupted_ai_runs()
    RECALL_JUDGE.start()
    try:
        ThreadingHTTPServer((HOST, PORT), H).serve_forever()
    finally:
        RECALL_JUDGE.stop()
