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

PANEL_DIR = Path(__file__).resolve().parents[1] / "panel"
PANEL_SERVER = PANEL_DIR / "server.py"
if str(PANEL_DIR) not in sys.path:
    sys.path.insert(0, str(PANEL_DIR))


def _load_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    explicit_api_key: str | None = None,
):
    keys = tmp_path / "panel-keys.json"
    keys.write_text('{"vanilla":"legacy-test-placeholder"}', encoding="utf-8")
    api_keys = tmp_path / "api_keys.yaml"
    api_keys.write_text(
        """
api_keys:
  - key_id: key_panel_test
    api_key: canonical-test-placeholder
    project_id: proj_panel_test
    memory_algorithm: vanilla
    enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    runtime_config = tmp_path / "runtime.yaml"
    runtime_config.write_text(
        f"auth:\n  mode: api_key\n  api_key_file: {api_keys}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDMEMOS_CONFIG_PATH", str(runtime_config))
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(keys))
    if explicit_api_key is None:
        monkeypatch.delenv("MINDMEMOS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MINDMEMOS_API_KEY", explicit_api_key)
    monkeypatch.setenv("MM_TURN_LEDGER", str(tmp_path / "panel-ledger.sqlite3"))
    monkeypatch.setenv("MM_MODEL_ENDPOINTS_PATH", str(tmp_path / "model-endpoints.json"))
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


def test_panel_api_key_uses_runtime_auth_config_before_legacy_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)

    assert panel.MM_KEY == "canonical-test-placeholder"
    assert panel.MM_KEY_SOURCE == str(tmp_path / "api_keys.yaml")


def test_panel_explicit_bad_api_key_is_not_silently_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _load_panel(tmp_path, monkeypatch, explicit_api_key="known-bad-test-key")

    assert panel.MM_KEY == "known-bad-test-key"
    assert panel.MM_KEY_SOURCE == "MINDMEMOS_API_KEY"


def test_panel_api_key_falls_back_to_standard_provider_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_config = tmp_path / "mindmemos.json"
    provider_config.write_text('{"api_key":"provider-test-placeholder"}', encoding="utf-8")
    monkeypatch.delenv("MINDMEMOS_API_KEY", raising=False)
    monkeypatch.setenv("MINDMEMOS_CONFIG_PATH", str(tmp_path / "missing-runtime.yaml"))
    monkeypatch.setenv("MINDMEMOS_PANEL_KEYS", str(tmp_path / "missing-legacy-keys.json"))
    monkeypatch.setenv("MINDMEMOS_PROVIDER_CONFIG", str(provider_config))
    spec = importlib.util.spec_from_file_location("test_mm_panel_provider_fallback", PANEL_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    assert module.MM_KEY == "provider-test-placeholder"
    assert module.MM_KEY_SOURCE == str(provider_config)


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


def test_panel_uses_structured_message_source_when_document_name_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    rows = panel.clean_memories(
        [
            {
                "payload": {
                    "memory_id": "memory-source",
                    "content": "一条来自正常对话的长期事实。",
                    "mem_type": "fact",
                    "created_at": "2026-08-09T15:36:58+00:00",
                    "metadata": {
                        "source_id": "source-message-1",
                        "source_type": "message",
                        "source_role": "user",
                        "source_message_index": 0,
                        "source": "hermes_turn",
                    },
                }
            }
        ]
    )

    assert rows[0]["doc"] == "Hermes 对话 · 用户消息"
    assert rows[0]["source"] == {
        "id": "source-message-1",
        "type": "message",
        "role": "user",
        "message_index": 0,
        "channel": "hermes_turn",
        "label": "Hermes 对话 · 用户消息",
    }


def test_panel_attaches_ranked_topics_from_memory_entity_relationships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _load_panel(tmp_path, monkeypatch)
    rows = [{"id": "memory-topic", "entities": ["fallback-name"]}, {"id": "memory-none", "entities": []}]
    monkeypatch.setattr(
        panel,
        "cypher",
        lambda *_args, **_kwargs: [
            {"memory_id": "memory-topic", "entity_name": "Claude Code", "entity_type": "tool"},
            {"memory_id": "memory-topic", "entity_name": "MindMemOS", "entity_type": "project"},
            {"memory_id": "memory-topic", "entity_name": "记忆质量", "entity_type": "other"},
        ],
    )

    panel._attach_topics(rows)

    assert rows[0]["topics"] == [
        {"name": "MindMemOS", "type": "project"},
        {"name": "Claude Code", "type": "tool"},
        {"name": "记忆质量", "type": "other"},
    ]
    assert rows[0]["topic"] == "MindMemOS"
    assert rows[1]["topics"] == []
    assert rows[1]["topic"] == "未归类"


def test_panel_memory_card_renders_source_and_topic_fields() -> None:
    html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")

    assert "source.label||sourceLabel" in html
    assert "主题：" in html
    assert "m.topics" in html


def test_panel_search_cards_merge_source_and_topics_without_dereferencing_missing_source() -> None:
    html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")

    assert "source:hit&&hit.source" in html
    assert "topics:hit&&hit.topics" in html
    assert "m.source.label||sourceLabel" not in html
    assert "bottom:286px" in html


def test_panel_memory_list_excludes_archived_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _load_panel(tmp_path, monkeypatch)

    rows = panel.clean_memories(
        [
            {"payload": {"memory_id": "active", "status": "active", "content": "保留"}},
            {"payload": {"memory_id": "archived", "status": "archived", "content": "不应展示"}},
        ]
    )

    assert [row["id"] for row in rows] == ["active"]


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


def test_panel_frontend_exposes_one_latest_view_with_full_browse_capability() -> None:
    html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="refresh-all"' in html
    assert "setInterval(checkForUpdates,30000)" in html
    assert 'id="dashboard"' in html
    assert "renderDashboard(s,RECENT)" in html
    assert 'id="rules-edit"' in html
    assert "fetch('/api/rules'" in html
    assert 'data-t="models"' in html
    assert 'class="tabs top-tabs"' in html
    assert 'class="tab on" data-t="home"' in html
    assert html.count('data-t="recent"') == 1
    assert '<div class="tab" data-t="recent">最新新增</div>' in html
    assert 'data-t="browse"' not in html
    assert 'id="pane-browse"' not in html
    assert 'id="pane-recent"' in html
    assert 'id="latest-day-filter"' in html
    assert 'id="latest-day-clear"' in html
    assert "filterLatestRows(ALL,SELECTED_DAY)" in html
    assert "rows.slice(0,SHOWN)" in html
    assert "SHOWN+=60" in html
    assert "if(k==='recent')setTerminalCompact(true)" in html
    assert 'data-t="search"' not in html
    assert 'id="pane-search"' not in html
    assert html.index('class="tabs top-tabs"') < html.index('id="pane-home"')
    assert "grid-template-columns:repeat(8,minmax(0,1fr));overflow:visible" in html
    assert 'id="pane-models"' in html
    assert 'class="memory-terminal floating-terminal"' in html
    assert 'id="search-results-layer"' in html
    assert 'class="terminal-footer"' in html
    assert html.index('id="memory-terminal"') < html.index('id="ex"')
    refresh_button = html.split('id="refresh-all"', 1)[1].split("</button>", 1)[0]
    refresh_button_content = refresh_button.split(">", 1)[1]
    assert "刷新数据" not in refresh_button_content
    assert 'data-lucide="refresh-cw"' in refresh_button_content
    assert 'id="q"' not in html
    assert 'id="go"' not in html
    assert "||'home'" in html
    assert "e.key==='Enter'&&!e.shiftKey" in html
    assert "$('#terminal-input').value=e.target.textContent;runMemoryCommand()" in html
    assert 'id="model-llm-name"' in html
    assert 'id="model-embedding-name"' in html
    assert 'id="model-rerank-name"' in html
    assert 'id="endpoint-list"' in html
    assert 'id="endpoint-add"' in html
    assert 'id="endpoint-refresh-all"' in html
    assert 'id="endpoint-edit-url"' in html
    assert 'id="endpoint-edit-key"' in html
    assert 'role="combobox"' in html
    assert 'id="model-llm-options" role="listbox"' in html
    assert 'id="model-embedding-options" role="listbox"' in html
    assert 'id="model-rerank-options" role="listbox"' in html
    registry_js = (PANEL_DIR / "model-registry.js").read_text(encoding="utf-8")
    assert "fetch('/api/model-endpoints'" in registry_js
    assert "fetch('/api/model-endpoints/'+path" in registry_js
    assert "CACHED_MODELS" in registry_js
    assert "fetch('/api/models/list'" not in registry_js
    assert "endpoint_id:s.endpoint_id" in registry_js
    assert "model_id:s.id" in registry_js
    assert "fetch('/api/models/test'" in registry_js
    assert "fetch('/api/models'" in registry_js
    assert "setTimeout(()=>{btn.disabled=false;renderUpList()" not in html
    assert "UP_FILES=[]; fi.value=''; btn.disabled=true" in html
    assert '<script src="/dashboard.js"></script>' in html
    assert 'id="memory-chart-path"' in html
    assert "ResizeObserver" in html
    assert "path.animate" not in html
    assert 'id="memory-terminal"' in html
    assert "UNKNOWN OR UNSAFE COMMAND" in html
    assert "runMemoryCommand" in html
    assert 'type="password"' in html
    assert "API Key 只写入服务器配置，页面永远不会回显" in html

    server = PANEL_SERVER.read_text(encoding="utf-8")
    assert 'LLMS_URL = os.getenv("MM_LLMS_URL"' in server
    assert 'self.send_header("Location", LLMS_URL)' in server
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
    monkeypatch.setenv("MM_MODEL_ENDPOINTS_PATH", str(tmp_path / "model-endpoints.json"))
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
        assert current["models"]["llm"]["model"] == "openai/old-chat"
        assert current["models"]["llm"]["endpoint_id"].startswith("ep_")
        assert "api_key" not in json.dumps(current)

        catalog_calls = []

        def fake_catalog(endpoint: str, api_key: str) -> set[str]:
            catalog_calls.append((endpoint, api_key))
            return {"new-chat", "old-chat"}

        monkeypatch.setattr(panel, "_provider_model_ids", fake_catalog)
        endpoint_payload = json.dumps(
            {
                "name": "Catalog Hub",
                "endpoint": "https://catalog.example/v1",
                "api_key": "temporary-list-key",
            }
        ).encode()
        conn.request(
            "POST",
            "/api/model-endpoints/save",
            body=endpoint_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(endpoint_payload))},
        )
        response = conn.getresponse()
        catalog = json.loads(response.read())
        assert response.status == 200
        catalog_endpoint = next(item for item in catalog["endpoints"] if item["name"] == "Catalog Hub")
        assert catalog_endpoint["model_count"] == 2
        assert {item["id"] for item in catalog["catalog"] if item["endpoint_id"] == catalog_endpoint["id"]} == {
            "new-chat",
            "old-chat",
        }
        assert "temporary-list-key" not in json.dumps(catalog)
        assert catalog_calls == [("https://catalog.example/v1", "temporary-list-key")]

        for _ in range(2):
            conn.request("GET", "/api/model-endpoints")
            response = conn.getresponse()
            cached = json.loads(response.read())
            assert response.status == 200
            assert cached["catalog"] == catalog["catalog"]
        assert catalog_calls == [("https://catalog.example/v1", "temporary-list-key")]

        refresh_body = json.dumps({"id": catalog_endpoint["id"]}).encode()
        conn.request(
            "POST",
            "/api/model-endpoints/refresh",
            body=refresh_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(refresh_body))},
        )
        response = conn.getresponse()
        refreshed = json.loads(response.read())
        assert response.status == 200
        assert refreshed["results"][0]["ok"] is True
        assert len(catalog_calls) == 2

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
    registry_path = tmp_path / "model-endpoints.json"
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert any(item["models"] == ["new-chat", "old-chat"] for item in registry_data["endpoints"])


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
