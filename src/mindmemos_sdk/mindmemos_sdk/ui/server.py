"""Dependency-free local server for the SDK console and its JSON API."""

from __future__ import annotations

import functools
import http.server
import json
import secrets
import threading
import webbrowser
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import ConfigManager, SDKConfig, mask_secret
from ..errors import ConfigError, MindMemOSSDKError
from ..memory import MemoryClient
from ..memory.core import MemoryDefaults
from ..skills import (
    ExportSkillRequest,
    LocalSkillRepository,
    PublishLocalRequest,
    RegisterLocalRequest,
    SkillCloudClient,
    SkillManager,
)
from ..transport import HttpTransport
from .lite_trace_service import LiteTraceService
from .skill_service import LocalSkillUIService


class _LocalUIHandler(http.server.SimpleHTTPRequestHandler):
    """Serve packaged assets and a small local-only JSON API."""

    server_version = "MindMemOSLocalUI/0.1"

    def __init__(
        self,
        *args: object,
        config_manager: ConfigManager,
        launch_token: str,
        **kwargs: object,
    ) -> None:
        self._config_manager = config_manager
        self._launch_token = launch_token
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._handle_api_get(path)
            return
        super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._validate_mutation_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/v1/config":
            self._handle_config_update()
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/content"):
            self._send_json(
                {
                    "error": "immutable_version",
                    "message": "Existing Skill versions are immutable. Publish an editor draft instead.",
                },
                status=409,
            )
            return
        self._send_json({"error": "not_found", "message": "Unknown API route."}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._validate_mutation_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/v1/skills/register":
            self._handle_skill_register()
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/publish"):
            self._handle_skill_publish(path.removesuffix("/publish"))
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/switch"):
            self._handle_skill_switch(path.removesuffix("/switch"))
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/export"):
            self._handle_skill_export(path.removesuffix("/export"))
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/sync"):
            self._handle_skill_sync(path.removesuffix("/sync"))
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/evolve"):
            self._handle_skill_evolve(path.removesuffix("/evolve"))
            return
        if path.startswith("/api/v1/skills/") and path.endswith("/promote"):
            self._handle_skill_promote(path.removesuffix("/promote"))
            return
        self._send_json({"error": "not_found", "message": "Unknown API route."}, status=404)

    def _handle_api_get(self, path: str) -> None:
        try:
            if path == "/api/v1/health":
                self._send_json({"ok": True, "service": "mindmemos-sdk-ui"})
                return
            if path == "/api/v1/config":
                self._send_json(_config_payload(self._config_manager))
                return
            if path == "/api/v1/skills":
                self._send_json(_skills_payload(self._config_manager))
                return
            if path in {"/api/v1/memories", "/api/v1/memories/search"}:
                self._handle_memory_get(path)
                return
            if path == "/api/v1/lite/traces" or path.startswith("/api/v1/lite/traces/"):
                if not self._validate_local_request():
                    return
                self._handle_lite_trace_get(path)
                return
            if path.startswith("/api/v1/skills/"):
                self._handle_skill_get(path)
                return
            self._send_json({"error": "not_found", "message": "Unknown API route."}, status=404)
        except (ConfigError, MindMemOSSDKError, OSError, ValueError) as exc:
            self._send_json({"error": "sdk_error", "message": str(exc)}, status=400)

    def _handle_memory_get(self, path: str) -> None:
        query = parse_qs(urlsplit(self.path).query)
        client, transport, config = _memory_client(self._config_manager)
        try:
            user_id = config.defaults.user_id
            if not user_id:
                raise ValueError("Configure a User ID in Settings before loading Memory.")
            top_k = _query_top_k(query)
            if path.endswith("/search"):
                search_query = (query.get("q") or query.get("query") or [""])[0].strip()
                if not search_query:
                    raise ValueError("A search query is required.")
                kwargs: dict[str, object] = {"user_id": user_id}
                if top_k is not None:
                    kwargs["top_k"] = top_k
                result = client.search(search_query, **kwargs)
                mode = "search"
            else:
                kwargs = {"filters": _owned_memory_filters(config.memory.get_filters, user_id)}
                if top_k is not None:
                    kwargs["top_k"] = top_k
                result = client.get(**kwargs)
                mode = "list"
            self._send_json(
                {
                    "memories": [item.model_dump(mode="json") for item in result.memories],
                    "count": len(result.memories),
                    "mode": mode,
                    "user_id": user_id,
                    "request_id": result.request_id,
                }
            )
        finally:
            transport.close()

    def _handle_lite_trace_get(self, path: str) -> None:
        query = parse_qs(urlsplit(self.path).query)
        directory = (query.get("directory") or [""])[0]
        service = LiteTraceService()
        if path == "/api/v1/lite/traces":
            self._send_json(
                service.list_traces(
                    directory,
                    limit=_query_bounded_int(query, "limit", default=100, minimum=1, maximum=500),
                    offset=_query_bounded_int(query, "offset", default=0, minimum=0, maximum=1_000_000),
                )
            )
            return

        trace_id = unquote(path.removeprefix("/api/v1/lite/traces/")).strip()
        if not trace_id or "/" in trace_id:
            raise ValueError("A valid trace ID is required.")
        source = (query.get("source") or [""])[0]
        self._send_json(
            service.trace_detail(
                directory,
                source=source,
                trace_id=trace_id,
            )
        )

    def _handle_skill_get(self, path: str) -> None:
        suffix = path.removeprefix("/api/v1/skills/")
        parts = [unquote(part) for part in suffix.split("/") if part]
        if not parts:
            self._send_json({"error": "not_found", "message": "Skill reference is required."}, status=404)
            return
        skill_ref = parts[0]
        manager, transport = _skill_manager(self._config_manager)
        service = LocalSkillUIService(manager)
        try:
            if len(parts) == 1:
                self._send_json(service.detail(skill_ref).model_dump(mode="json"))
                return
            if parts[1] == "content":
                query = parse_qs(urlsplit(self.path).query)
                version_id = query.get("version_id", [None])[0]
                self._send_json(service.content(skill_ref, version_id).model_dump(mode="json"))
                return
            if parts[1] == "compare":
                query = parse_qs(urlsplit(self.path).query)
                from_version_id = (query.get("from") or [None])[0]
                to_version_id = (query.get("to") or [None])[0]
                if not from_version_id or not to_version_id:
                    raise ValueError("compare requires from and to version IDs")
                self._send_json(
                    service.compare(skill_ref, from_version_id, to_version_id).model_dump(mode="json")
                )
                return
            self._send_json({"error": "not_found", "message": "Unknown Skill route."}, status=404)
        finally:
            transport.close()

    def _handle_skill_register(self) -> None:
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            result = LocalSkillUIService(manager).register(RegisterLocalRequest.model_validate(payload))
            self._send_json(result.model_dump(mode="json"), status=201 if result.action == "created" else 200)
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_register_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_publish(self, path: str) -> None:
        suffix = path.removeprefix("/api/v1/skills/")
        parts = [unquote(part) for part in suffix.split("/") if part]
        if len(parts) != 1:
            self._send_json({"error": "not_found", "message": "Skill reference is required."}, status=404)
            return

        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Skill content must be a non-empty string.")
            request = PublishLocalRequest(
                skill_id=parts[0],
                base_version_id=_optional_string(payload, "base_version_id"),
                content=content,
                version_label=_optional_string(payload, "version_label"),
                commit_message=_optional_string(payload, "commit_message"),
                activate=bool(payload.get("activate", False)),
            )
            result, detail = LocalSkillUIService(manager).publish(request)
            self._send_json(
                {
                    "result": result.model_dump(mode="json"),
                    "detail": detail.model_dump(mode="json"),
                    "message": f"Published immutable local version {result.version_id}.",
                },
                status=201,
            )
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_publish_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_switch(self, path: str) -> None:
        skill_ref = _single_skill_ref(path)
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            version_id = payload.get("version_id")
            if not isinstance(version_id, str) or not version_id.strip():
                raise ValueError("version_id must be a non-empty string")
            detail = LocalSkillUIService(manager).switch(skill_ref, version_id.strip())
            self._send_json(detail.model_dump(mode="json"))
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_switch_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_export(self, path: str) -> None:
        skill_ref = _single_skill_ref(path)
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            target_path = payload.get("target_path")
            if not isinstance(target_path, str) or not target_path.strip():
                raise ValueError("target_path must be a non-empty string")
            result = LocalSkillUIService(manager).export(
                ExportSkillRequest(
                    skill_id=skill_ref,
                    target_path=target_path,
                    version_id=_optional_string(payload, "version_id"),
                    replace=bool(payload.get("replace", True)),
                )
            )
            self._send_json(result.model_dump(mode="json"))
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_export_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_sync(self, path: str) -> None:
        skill_ref = _single_skill_ref(path)
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            direction = payload.get("direction", "both")
            if not isinstance(direction, str):
                raise ValueError("direction must be a string")
            detail = LocalSkillUIService(manager).sync(
                skill_ref,
                direction=direction,
            )
            self._send_json(detail.model_dump(mode="json"))
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_sync_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_evolve(self, path: str) -> None:
        skill_ref = _single_skill_ref(path)
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            mode = payload.get("mode", "sync")
            if mode not in {"sync", "async"}:
                raise ValueError("mode must be 'sync' or 'async'")
            result = LocalSkillUIService(manager).evolve(
                skill_ref,
                base_version_id=_optional_string(payload, "base_version_id"),
                algorithm=_optional_string(payload, "algorithm"),
                mode=mode,
                operation_id=_optional_string(payload, "operation_id"),
            )
            self._send_json(result.model_dump(mode="json"))
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_evolve_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_skill_promote(self, path: str) -> None:
        skill_ref = _single_skill_ref(path)
        manager, transport = _skill_manager(self._config_manager)
        try:
            payload = self._read_json()
            version_id = _optional_string(payload, "version_id")
            if version_id is None:
                raise ValueError("version_id must be a non-empty string")
            revision = payload.get("expected_cloud_revision")
            if revision is not None and (
                not isinstance(revision, int) or isinstance(revision, bool)
            ):
                raise ValueError("expected_cloud_revision must be an integer")
            result = LocalSkillUIService(manager).promote(
                skill_ref,
                version_id=version_id,
                expected_cloud_revision=revision,
                operation_id=_optional_string(payload, "operation_id"),
            )
            self._send_json(result.model_dump(mode="json"))
        except (ConfigError, MindMemOSSDKError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": "skill_promote_failed", "message": str(exc)}, status=400)
        finally:
            transport.close()

    def _handle_config_update(self) -> None:
        try:
            payload = self._read_json()
            config = self._config_manager.load_or_default()
            _apply_config_update(config, payload)
            validated = SDKConfig.model_validate(config.model_dump())
            self._config_manager.save(validated)
            self._send_json(_config_payload(self._config_manager))
        except (ConfigError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json({"error": "invalid_config", "message": str(exc)}, status=400)

    def _read_json(self) -> dict[str, object]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ValueError("Content-Length is required.")
        length = int(length_header)
        if length > 2_000_000:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def _send_json(self, payload: object, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _validate_mutation_request(self) -> bool:
        return self._validate_local_request()

    def _validate_local_request(self) -> bool:
        supplied_token = self.headers.get("X-MindMemOS-UI-Token")
        if supplied_token is None or not secrets.compare_digest(supplied_token, self._launch_token):
            self._send_json({"error": "forbidden", "message": "Invalid local UI launch token."}, status=403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            server_port = self.server.server_address[1]
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port != server_port
            ):
                self._send_json({"error": "forbidden", "message": "Invalid local UI origin."}, status=403)
                return False
        return True


def _static_directory() -> Path:
    """Resolve the packaged static directory in a source tree or wheel."""
    return Path(files("mindmemos_sdk.ui").joinpath("static"))


def _config_payload(config_manager: ConfigManager) -> dict[str, object]:
    config = config_manager.load_or_default()
    return {
        "config_path": str(config_manager.config_path),
        "base_url": config.base_url,
        "api_key_configured": bool(config.auth.api_key),
        "api_key_masked": mask_secret(config.auth.api_key),
        "defaults": config.defaults.model_dump(mode="json"),
        "memory": config.memory.model_dump(mode="json"),
        "storage": config.storage.model_dump(mode="json"),
        "network": config.network.model_dump(mode="json"),
        "skills_count": len(LocalSkillRepository(config_manager).list_manifests()),
        "metadata": config.metadata.model_dump(mode="json"),
    }


def _apply_config_update(config: SDKConfig, payload: dict[str, object]) -> None:
    """Apply only UI-owned fields; an empty API key intentionally preserves it."""
    if isinstance(payload.get("base_url"), str) and payload["base_url"].strip():
        config.base_url = payload["base_url"].strip()

    api_key = payload.get("api_key")
    if isinstance(api_key, str) and api_key:
        config.auth.api_key = api_key

    for field in ("user_id", "app_id", "agent_id", "session_id"):
        value = payload.get(field)
        if value is not None:
            setattr(config.defaults, field, str(value).strip() or None)

    for field in ("skill_cache_dir", "skill_backup_dir"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            setattr(config.storage, field, value.strip())

    for field in ("timeout_seconds", "max_retries"):
        value = payload.get(field)
        if value is not None:
            setattr(config.network, field, int(value))

    memory = payload.get("memory")
    if isinstance(memory, dict):
        for field in (
            "search_top_k",
            "search_strategy",
            "search_rerank",
            "search_score_threshold",
            "search_filters",
            "add_mode",
            "add_default_role",
            "add_auto_skill_context",
            "get_top_k",
            "get_filters",
            "feedback_mode",
            "dreaming_mode",
        ):
            if field in memory:
                setattr(config.memory, field, memory[field])


def _skill_manager(config_manager: ConfigManager) -> tuple[SkillManager, HttpTransport]:
    config = config_manager.load_or_default()
    transport = HttpTransport(
        base_url=config.base_url,
        api_key=config.auth.api_key,
        timeout_seconds=config.network.timeout_seconds,
        max_retries=config.network.max_retries,
    )
    return SkillManager.from_config_manager(config_manager, SkillCloudClient(transport)), transport


def _memory_client(config_manager: ConfigManager) -> tuple[MemoryClient, HttpTransport, SDKConfig]:
    config = config_manager.load_or_default()
    transport = HttpTransport(
        base_url=config.base_url,
        api_key=config.auth.api_key,
        timeout_seconds=config.network.timeout_seconds,
        max_retries=config.network.max_retries,
    )
    defaults = MemoryDefaults(
        user_id=config.defaults.user_id,
        app_id=config.defaults.app_id,
        agent_id=config.defaults.agent_id,
        session_id=config.defaults.session_id,
        add_mode=config.memory.add_mode,
        add_default_role=config.memory.add_default_role,
        add_auto_skill_context=config.memory.add_auto_skill_context,
        search_top_k=config.memory.search_top_k,
        search_strategy=config.memory.search_strategy,
        search_rerank=config.memory.search_rerank,
        search_score_threshold=config.memory.search_score_threshold,
        search_filters=config.memory.search_filters,
        get_top_k=config.memory.get_top_k,
        get_filters=config.memory.get_filters,
        feedback_mode=config.memory.feedback_mode,
        dreaming_mode=config.memory.dreaming_mode,
    )
    return MemoryClient(transport, memory_defaults=defaults), transport, config


def _query_top_k(query: dict[str, list[str]]) -> int | None:
    raw = (query.get("top_k") or [""])[0].strip()
    if not raw:
        return None
    top_k = int(raw)
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")
    return top_k


def _query_bounded_int(
    query: dict[str, list[str]],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = (query.get(key) or [""])[0].strip()
    if not raw:
        return default
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _owned_memory_filters(filters: dict[str, object] | None, user_id: str) -> dict[str, object]:
    """Keep the local Memory page scoped to the configured user."""
    owner = {"user_id": user_id}
    if not filters:
        return owner
    return {"AND": [filters, owner]}


def _skills_payload(config_manager: ConfigManager) -> dict[str, object]:
    manager, transport = _skill_manager(config_manager)
    try:
        skills = LocalSkillUIService(manager).list_skills()
        pending = manager.local_repository.load_outbox().operations
        return {
            "skills": [item.model_dump(mode="json") for item in skills],
            "outbox_operations": [item.model_dump(mode="json") for item in pending],
            "skills_count": len(skills),
            "pending_count": len(pending),
        }
    finally:
        transport.close()


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    normalized = value.strip()
    return normalized or None


def _single_skill_ref(path: str) -> str:
    suffix = path.removeprefix("/api/v1/skills/")
    parts = [unquote(part) for part in suffix.split("/") if part]
    if len(parts) != 1:
        raise ValueError("Skill reference is required")
    return parts[0]


def run_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    config_dir: str | Path | None = None,
) -> None:
    """Serve the unified local UI and SDK-backed API until interrupted."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The local MindMemOS UI only supports loopback hosts.")
    static_dir = _static_directory()
    config_manager = ConfigManager(config_dir)
    launch_token = secrets.token_urlsafe(32)
    handler = functools.partial(
        _LocalUIHandler,
        directory=str(static_dir),
        config_manager=config_manager,
        launch_token=launch_token,
    )
    server = http.server.ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_address[1]}/?token={launch_token}"
    print(f"MindMemOS local UI: {url}")
    print("Press Ctrl-C to stop.")

    if open_browser:
        threading.Timer(0.15, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
