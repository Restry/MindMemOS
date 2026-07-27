"""HTTP integration tests for the centralized local Skill UI workflow."""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from mindmemos_sdk.config import ConfigManager
from mindmemos_sdk.ui import server


@contextmanager
def _running_ui(config_dir: Path) -> Iterator[tuple[httpx.Client, str]]:
    token = "test-launch-token"
    handler = functools.partial(
        server._LocalUIHandler,
        directory=str(server._static_directory()),
        config_manager=ConfigManager(config_dir=config_dir),
        launch_token=token,
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
        headers={"X-MindMemOS-UI-Token": token},
    )
    try:
        yield client, token
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        'name: route-demo\nversion: "1.0.0"\n\nInitial body\n',
        encoding="utf-8",
    )
    (source / "references").mkdir()
    (source / "references" / "private.md").write_text(
        "private reference\n",
        encoding="utf-8",
    )
    return source


def test_skill_ui_keeps_registration_feedback_local_and_duplicate_policy_explicit():
    static_dir = server._static_directory()
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")
    css = (static_dir / "app.css").read_text(encoding="utf-8")

    assert "Ask before deciding" not in html
    assert '<option value="">Do not register it (default)</option>' in html
    assert html.index('id="register-skill"') < html.index('id="register-skill-status"')
    assert html.index('id="register-skill-status"') < html.index('id="register-skill-path"')
    assert 'setRegisterSkillStatus(duplicateHint, "error")' in javascript
    assert 'change "If an identical snapshot already exists"' in javascript
    assert '"Register a separate Skill"' in javascript
    assert ".register-skill-status[data-tone=\"error\"]" in css
    assert ".checkbox-row" in css
    assert "white-space: nowrap" in css


def test_ui_http_register_publish_and_export_complete_snapshot(tmp_path):
    source = _source(tmp_path)
    export_dir = tmp_path / "exported"

    with _running_ui(tmp_path / "config") as (client, _token):
        registered_response = client.post(
            "/api/v1/skills/register",
            json={
                "source_path": str(source),
                "alias": "route-main",
                "version_label": "root",
                "commit_message": "Initial UI registration",
            },
        )
        assert registered_response.status_code == 201
        registered = registered_response.json()

        detail = client.get(f"/api/v1/skills/{registered['skill_id']}").json()
        assert detail["skill"]["active_version_id"] == registered["version_id"]
        assert detail["versions"][0]["commit_message"] == "Initial UI registration"
        assert detail["versions"][0]["has_linked_files"] is True

        published_response = client.post(
            f"/api/v1/skills/{registered['skill_id']}/publish",
            json={
                "base_version_id": registered["version_id"],
                "content": 'name: route-demo\nversion: "1.1.0"\n\nPublished in UI\n',
                "version_label": "candidate",
                "commit_message": "Editor child version",
                "activate": False,
            },
        )
        assert published_response.status_code == 201
        published = published_response.json()
        version_id = published["result"]["version_id"]
        assert published["detail"]["skill"]["active_version_id"] == registered["version_id"]
        assert published["detail"]["versions"][-1]["commit_message"] == "Editor child version"

        exported_response = client.post(
            f"/api/v1/skills/{registered['skill_id']}/export",
            json={
                "target_path": str(export_dir),
                "version_id": version_id,
                "replace": True,
            },
        )
        assert exported_response.status_code == 200
        assert exported_response.json()["exported_files"] == [
            "SKILL.md",
            "references/private.md",
        ]

    assert (export_dir / "SKILL.md").read_text(encoding="utf-8").endswith(
        "Published in UI\n"
    )
    assert (export_dir / "references" / "private.md").read_text(
        encoding="utf-8"
    ) == "private reference\n"


def test_ui_http_duplicate_registration_requires_explicit_choice(tmp_path):
    source = _source(tmp_path)

    with _running_ui(tmp_path / "config") as (client, _token):
        first = client.post(
            "/api/v1/skills/register",
            json={"source_path": str(source)},
        )
        assert first.status_code == 201

        undecided = client.post(
            "/api/v1/skills/register",
            json={"source_path": str(source)},
        )
        assert undecided.status_code == 400
        assert "duplicate_action" in undecided.json()["message"]

        reused = client.post(
            "/api/v1/skills/register",
            json={
                "source_path": str(source),
                "duplicate_action": "reuse",
            },
        )
        assert reused.status_code == 200
        assert reused.json()["action"] == "reused"
        assert reused.json()["skill_id"] == first.json()["skill_id"]


def test_ui_http_mutation_requires_launch_token(tmp_path):
    source = _source(tmp_path)

    with _running_ui(tmp_path / "config") as (client, _token):
        response = client.post(
            "/api/v1/skills/register",
            headers={"X-MindMemOS-UI-Token": "wrong"},
            json={"source_path": str(source)},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
