from __future__ import annotations

import http.client
import importlib.util
import json
import stat
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

PANEL_SERVER = Path("/Users/leway/Projects/mm-panel/server.py")


def _load_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    keys = tmp_path / "panel-keys.json"
    keys.write_text('{"vanilla":"test-only-placeholder"}', encoding="utf-8")
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(keys))
    monkeypatch.setenv("MM_TURN_LEDGER", str(tmp_path / "panel-ledger.sqlite3"))
    spec = importlib.util.spec_from_file_location("test_mm_panel_server", PANEL_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_panel_deployment_paths_and_identity_are_environment_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = tmp_path / "panel-keys.json"
    keys.write_text('{"vanilla":"company-test-key"}', encoding="utf-8")
    state = tmp_path / "company-state"
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(keys))
    monkeypatch.setenv("MINDMEMOS_ROOT", str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("MINDMEMOS_STATE_DIR", str(state))
    monkeypatch.setenv("MINDMEMOS_USER", "company")
    monkeypatch.setenv("MM_PANEL_HOST", "192.168.1.235")
    monkeypatch.setenv("MM_MCP_TOKEN_STORE", str(state / "tokens.json"))
    monkeypatch.setenv("MM_MCP_LEGACY_TOKEN", str(state / "legacy-token"))

    panel = _load_panel(tmp_path, monkeypatch)

    assert panel.USER_ID == "company"
    assert panel.HOST == "192.168.1.235"
    assert panel.PINNED_PATH == str(state / "mindmemos_pinned.md")
    assert panel._MM_ROOT == str(Path(__file__).resolve().parents[1])
    assert panel.mcp_tokens.STORE == str(state / "tokens.json")
    assert panel.mcp_tokens.LEGACY == str(state / "legacy-token")


def test_panel_api_key_falls_back_to_standard_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_config = tmp_path / "mindmemos.json"
    provider_config.write_text('{"api_key":"provider-test-placeholder"}', encoding="utf-8")
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(tmp_path / "missing-legacy-keys.json"))
    monkeypatch.setenv("MINDMEMOS_PROVIDER_CONFIG", str(provider_config))
    spec = importlib.util.spec_from_file_location("test_mm_panel_provider_fallback", PANEL_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    assert module.MM_KEY == "provider-test-placeholder"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.8",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.223",
        "192.168.1.246",
        "fd00::1234",
    ],
)
def test_token_management_allows_localhost_and_private_lan(
    address: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    assert panel._is_lan(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "203.0.113.4", "invalid"])
def test_token_management_rejects_non_lan_sources(
    address: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    assert not panel._is_lan(address)


def test_panel_attaches_batched_multi_source_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    first = {
        "client_id": "hermes-fries",
        "agent_kind": "hermes",
        "instance": "macmini",
        "credential_id": "credential-a",
        "display_name": "Fries",
        "scope": "write",
        "authority": "credential",
    }
    second = {
        "client_id": "claude-fries",
        "agent_kind": "claude_code",
        "instance": "macmini",
        "credential_id": "credential-b",
        "display_name": "Fries",
        "scope": "write",
        "authority": "credential",
    }
    panel.provenance_ledger.record_response(
        {
            "code": "ok",
            "data": {"memories": [{"memory_id": "memory-panel", "operation": "add", "content": "fact"}]},
        },
        first,
        capture_mode="auto_hook",
        event_id="event-a",
        occurred_at=100,
    )
    panel.provenance_ledger.record_response(
        {
            "code": "ok",
            "data": {"memories": [{"memory_id": "memory-panel", "operation": "update", "content": "fact"}]},
        },
        second,
        capture_mode="explicit_remember",
        event_id="event-b",
        occurred_at=200,
    )

    rows = [{"id": "memory-panel"}]
    panel._attach_provenance(rows)
    contributors = rows[0]["provenance"]["contributors"]
    assert {item["client_id"] for item in contributors} == {
        "hermes-fries",
        "claude-fries",
    }


def test_panel_rules_are_validated_backed_up_and_atomically_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    rules_path = tmp_path / "mindmemos_pinned.md"
    rules_path.write_text("原来的行为准则必须保留备份。\n", encoding="utf-8")
    monkeypatch.setattr(panel, "PINNED_PATH", str(rules_path))

    rules = ["第一条行为准则至少八个字符。", "第二条行为准则同样足够明确。"]
    panel._write_rules(rules)

    assert panel._read_rules() == rules
    assert (tmp_path / "mindmemos_pinned.md.previous").read_text(encoding="utf-8") == ("原来的行为准则必须保留备份。\n")
    assert stat.S_IMODE(rules_path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="至少 8 个字符"):
        panel._write_rules(["太短"])
    assert panel._read_rules() == rules


def test_panel_recent_snapshot_uses_beijing_today_and_zero_fills_thirty_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    now = datetime(2026, 8, 6, 9, 0, tzinfo=panel.BEIJING_TZ)
    rows = [
        {"id": "a", "created": "2026-08-05T14:00:00+00:00"},
        {"id": "b", "created": "2026-08-03T12:00:00+00:00"},
    ]

    snapshot = panel._recent_snapshot(rows, now=now)

    assert snapshot["today"] == "2026-08-06"
    assert list(snapshot["by_day"])[0] == "2026-08-06"
    assert len(snapshot["by_day"]) == 30
    assert snapshot["by_day"]["2026-08-06"] == 0
    assert snapshot["by_day"]["2026-08-05"] == 1
    assert snapshot["by_day"]["2026-08-04"] == 0
    assert [row["id"] for row in snapshot["items"]] == ["a", "b"]


def test_panel_version_ignores_queue_churn_and_changes_after_successful_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    monkeypatch.setattr(panel, "PANEL_VERSION_PATH", str(tmp_path / "panel.version"))
    principal = {
        "client_id": "hermes-test",
        "agent_kind": "hermes",
        "instance": "test",
        "credential_id": "credential-test",
        "display_name": "Test",
        "scope": "write",
        "authority": "credential",
    }
    before = panel._data_version()
    panel.provenance_ledger.submit_turn(
        {
            "event_id": "queued-only",
            "session_id": "session",
            "turn_id": "turn-1",
            "user_message": "durable user message",
            "assistant_message": "durable answer",
            "started_at": "2026-08-06T00:00:00Z",
            "completed_at": "2026-08-06T00:00:01Z",
            "safe_context": {},
        },
        principal,
    )
    queued = panel._data_version()
    assert queued == before

    panel.provenance_ledger.record_response(
        {
            "code": "ok",
            "data": {"memories": [{"memory_id": "memory-version", "operation": "add", "content": "fact"}]},
        },
        principal,
        capture_mode="auto_hook",
        event_id="committed",
    )
    committed = panel._data_version()
    assert committed["memory_revision"] > before["memory_revision"]

    panel._bump_data_version()
    panel_change = panel._data_version()
    assert panel_change["panel_revision"] > committed["panel_revision"]


def test_panel_frontend_exposes_dashboard_refresh_and_rule_editor() -> None:
    html = Path("/Users/leway/Projects/mm-panel/index.html").read_text(encoding="utf-8")

    assert 'id="refresh-all"' in html
    assert "setInterval(checkForUpdates,30000)" in html
    assert 'id="dashboard"' in html
    assert "renderDashboard(s,RECENT)" in html
    assert 'id="rules-edit"' in html
    assert "fetch('/api/rules'" in html
    assert "fetch('/api/recent'" not in html
    assert "setTimeout(()=>loadWho(1)" not in html
    assert "$('#rule-cancel').onclick=()=>loadWho()" not in html
    assert 'data-t="models"' in html
    assert 'id="pane-models"' in html
    assert 'id="model-llm-name"' in html
    assert 'id="model-embedding-name"' in html
    assert 'id="model-rerank-name"' in html
    assert 'id="model-rerank-key"' in html
    assert "fetch('/api/models/test'" in html
    assert "fetch('/api/models'" in html
    assert "api_key:$('#model-'+k+'-key')" in html
    assert "setTimeout(()=>{btn.disabled=false;renderUpList()" not in html
    assert "UP_FILES=[]; fi.value=''; btn.disabled=true" in html
    assert 'id="memory-chart-path"' in html
    assert "getPointAtLength" in html
    assert "duration=6000" in html
    assert 'id="memory-terminal"' in html
    assert "UNKNOWN OR UNSAFE COMMAND" in html
    assert "runMemoryCommand" in html
    assert 'type="password"' in html
    assert "API Key 只写入服务器配置，页面永远不会回显" in html

    server = Path("/Users/leway/Projects/mm-panel/server.py").read_text(encoding="utf-8")
    assert 'LLMS_URL = os.getenv("MM_LLMS_URL"' in server
    assert "self.send_header('Location', LLMS_URL)" in server
    assert "open(os.path.join(HERE, 'llms.txt')" not in server


def test_panel_extractor_uses_configured_mindmemos_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    setattr(panel, "_EXTRACTOR", None)
    extractor = panel._load_extractor()
    assert extractor.__file__ == str(Path(__file__).resolve().parents[1] / "scripts/ingest/extractor.py")


def test_panel_model_settings_api_masks_keys_preserves_blank_keys_and_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = tmp_path / "panel-keys.json"
    keys.write_text('{"vanilla":"panel-test-key"}', encoding="utf-8")
    config = tmp_path / "dev.yaml"
    config.write_text(
        """
chat_model_router:
  endpoints:
    - model: openai/old-chat
      api_base: https://old.example/v1
      api_key: old-chat-key
      timeout: 99
embed_model_router:
  endpoints:
    - model: openai/old-embed
      api_base: https://old.example/v1
      api_key: old-embed-key
      dimensions: 1024
rerank_model_router:
  endpoints:
    - model: jina_ai/old-rerank
      api_base: https://old.example/v1
      api_key: old-rerank-key
database:
  qdrant:
    vector_size: 1024
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(keys))
    monkeypatch.setenv("MM_MODEL_CONFIG_PATH", str(config))
    monkeypatch.setenv("MM_MODEL_CONFIG_BACKUP_DIR", str(tmp_path / "backups"))
    panel = _load_panel(tmp_path, monkeypatch)
    monkeypatch.setattr(panel, "_reload_model_services", lambda: {"reloaded": True})

    server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/api/models")
        response = conn.getresponse()
        current = json.loads(response.read())
        assert response.status == 200
        assert current["models"]["llm"] == {
            "model": "openai/old-chat",
            "endpoint": "https://old.example/v1",
            "key_configured": True,
        }
        assert "api_key" not in json.dumps(current)

        payload = {
            "models": {
                "llm": {
                    "model": "openai/new-chat",
                    "endpoint": "https://hub.example/v1",
                    "api_key": "new-chat-key",
                },
                "embedding": {
                    "model": "openai/new-embed",
                    "endpoint": "https://hub.example/v1",
                    "api_key": "",
                },
                "rerank": {
                    "model": "jina_ai/new-rerank",
                    "endpoint": "https://hub.example/v1",
                    "api_key": "",
                },
            }
        }
        body = json.dumps(payload).encode()
        conn.request(
            "POST",
            "/api/models",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = conn.getresponse()
        saved = json.loads(response.read())
        assert response.status == 200
        assert saved["ok"] is True
        assert saved["reloaded"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    written = panel.yaml.safe_load(config.read_text(encoding="utf-8"))
    assert written["chat_model_router"]["endpoints"][0]["api_key"] == "new-chat-key"
    assert written["embed_model_router"]["endpoints"][0]["api_key"] == "old-embed-key"
    assert written["rerank_model_router"]["endpoints"][0]["api_key"] == "old-rerank-key"
    assert written["chat_model_router"]["endpoints"][0]["timeout"] == 99
    assert written["database"]["qdrant"]["vector_size"] == 1024
    backups = list((tmp_path / "backups").glob("dev.*.yaml"))
    assert len(backups) == 1
    assert "old-chat-key" in backups[0].read_text(encoding="utf-8")


def test_panel_model_connection_test_uses_current_key_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class ProviderHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer current-key":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "data": [
                        {"id": "chat-model"},
                        {"id": "embed-model"},
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/v1/rerank" or self.headers.get("Authorization") != "Bearer current-key":
                self.send_response(401)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n))
            assert payload["model"] == "BAAI/rerank-model"
            body = json.dumps({"results": [{"index": 0, "relevance_score": 1.0}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    base = f"http://127.0.0.1:{provider.server_port}/v1"

    keys = tmp_path / "panel-keys.json"
    keys.write_text('{"vanilla":"panel-test-key"}', encoding="utf-8")
    config = tmp_path / "dev.yaml"
    config.write_text(
        f"""
chat_model_router:
  endpoints:
    - model: openai/chat-model
      api_base: {base}
      api_key: current-key
embed_model_router:
  endpoints:
    - model: openai/embed-model
      api_base: {base}
      api_key: current-key
rerank_model_router:
  endpoints:
    - model: jina_ai/BAAI/rerank-model
      api_base: {base}
      api_key: current-key
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(keys))
    monkeypatch.setenv("MM_MODEL_CONFIG_PATH", str(config))
    panel = _load_panel(tmp_path, monkeypatch)
    server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.H)
    panel_thread = threading.Thread(target=server.serve_forever, daemon=True)
    panel_thread.start()
    payload = {
        "models": {
            "llm": {"model": "openai/chat-model", "endpoint": base, "api_key": ""},
            "embedding": {"model": "openai/embed-model", "endpoint": base, "api_key": ""},
            "rerank": {"model": "jina_ai/BAAI/rerank-model", "endpoint": base, "api_key": ""},
        }
    }
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps(payload).encode()
        conn.request(
            "POST",
            "/api/models/test",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = conn.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result["ok"] is True
        assert all(item["ok"] for item in result["results"].values())
        assert "current-key" not in json.dumps(result)
        assert config.read_text(encoding="utf-8").count("current-key") == 3
    finally:
        server.shutdown()
        server.server_close()
        panel_thread.join(timeout=5)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
