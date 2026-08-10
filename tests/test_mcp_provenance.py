from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mcp_tokens
from turn_ingest import IdempotencyConflict, TurnLedger


def _principal(client: str, kind: str, instance: str, credential: str) -> mcp_tokens.Principal:
    return mcp_tokens.Principal(
        client_id=client,
        agent_kind=kind,
        instance=instance,
        credential_id=credential,
        display_name=instance,
        scope="write",
    )


def _turn(event_id: str, *, user: str = "user fact", assistant: str = "assistant answer") -> dict:
    return {
        "event_id": event_id,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "user_message": user,
        "assistant_message": assistant,
        "started_at": "2026-08-05T10:00:00Z",
        "completed_at": "2026-08-05T10:00:01Z",
        "safe_context": {"runtime": "test"},
    }


def _response(memory_id: str, operation: str = "add", related: list[str] | None = None) -> dict:
    return {
        "code": "ok",
        "data": {
            "memories": [
                {
                    "memory_id": memory_id,
                    "operation": operation,
                    "content": "test memory",
                    "related_memory_ids": related or [],
                }
            ]
        },
    }


def test_ledger_connection_closes_after_success(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"))

    with ledger._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_ledger_connection_rolls_back_and_closes_after_error(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"))
    with ledger._connect() as setup_connection:
        setup_connection.execute("CREATE TABLE rollback_probe (value TEXT NOT NULL)")

    with pytest.raises(RuntimeError, match="force rollback"):
        with ledger._connect() as failed_connection:
            failed_connection.execute("INSERT INTO rollback_probe VALUES ('uncommitted')")
            raise RuntimeError("force rollback")

    with ledger._connect() as verification_connection:
        assert verification_connection.execute("SELECT COUNT(*) FROM rollback_probe").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_connection.execute("SELECT 1")


def test_existing_and_legacy_tokens_resolve_explicit_fallback_principals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "tokens.json"
    legacy = tmp_path / "legacy"
    old_token = "old-record-token"
    legacy_token = "legacy-file-token"
    store.write_text(
        json.dumps(
            [
                {
                    "id": "abc123",
                    "name": "old laptop",
                    "hash": mcp_tokens._hash(old_token),
                    "scope": "write",
                    "revoked": False,
                }
            ]
        )
    )
    legacy.write_text(legacy_token)
    monkeypatch.setattr(mcp_tokens, "STORE", str(store))
    monkeypatch.setattr(mcp_tokens, "LEGACY", str(legacy))

    old = mcp_tokens.authenticate(old_token, "write")
    assert old.ok
    assert old.principal is not None
    assert old.principal.client_id == "legacy-record-abc123"
    assert old.principal.authority == "legacy_fallback"

    legacy_result = mcp_tokens.authenticate(legacy_token, "write")
    assert legacy_result.ok
    assert legacy_result.principal == mcp_tokens.legacy_principal()
    assert "hash" not in mcp_tokens.listing()[0]


def test_readonly_denial_and_rotation_keep_stable_client_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_tokens, "STORE", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(mcp_tokens, "LEGACY", str(tmp_path / "missing-legacy"))

    first = mcp_tokens.issue(
        "claude first key",
        "write",
        client_id="claude-fries",
        agent_kind="claude_code",
        instance="macmini",
        display_name="Fries",
    )
    rotated = mcp_tokens.issue(
        "claude rotated key",
        "read",
        client_id="claude-fries",
        agent_kind="claude_code",
        instance="macmini",
    )

    first_auth = mcp_tokens.authenticate(first["token"], "write")
    rotated_read = mcp_tokens.authenticate(rotated["token"], "read")
    rotated_write = mcp_tokens.authenticate(rotated["token"], "write")
    assert first_auth.principal is not None
    assert rotated_read.principal is not None
    assert first_auth.principal.client_id == rotated_read.principal.client_id == "claude-fries"
    assert not rotated_write.ok
    assert rotated_write.reason == "scope"


def test_ingestion_is_idempotent_and_uses_server_principal(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"), base_backoff_seconds=0)
    assert oct(Path(ledger.path).stat().st_mode & 0o777) == "0o600"
    principal = _principal("hermes-fries", "hermes", "macmini", "credential-1")
    payload = _turn("evt-idempotent")

    first = ledger.submit_turn(payload, principal)
    duplicate = ledger.submit_turn(payload, principal)
    assert first.created
    assert not duplicate.created
    assert duplicate.status == "pending"
    with pytest.raises(IdempotencyConflict):
        ledger.submit_turn({**payload, "assistant_message": "different"}, principal)

    delivered: list[dict] = []

    def deliver(path: str, body: dict) -> dict:
        assert path == "/v1/memory/add"
        delivered.append(body)
        return _response("memory-1")

    result = ledger.process_next(deliver, event_id=payload["event_id"], force=True)
    assert result is not None and result.status == "done"
    assert len(delivered) == 1
    body = delivered[0]
    assert body["app_id"] == "hermes-fries"
    assert body["agent_id"] == "hermes:macmini"
    assert body["metadata"]["provenance"]["capture_mode"] == "auto_hook"
    assert body["metadata"]["provenance"]["credential_id"] == "credential-1"
    assert ledger.process_next(deliver, event_id=payload["event_id"], force=True).status == "done"
    assert len(delivered) == 1


def test_failed_delivery_survives_restart_and_retries(tmp_path: Path) -> None:
    path = str(tmp_path / "ledger.sqlite3")
    principal = _principal("omp-fries", "omp", "macmini", "credential-2")
    first = TurnLedger(path, base_backoff_seconds=0)
    first.submit_turn(_turn("evt-retry"), principal)

    failed = first.process_next(
        lambda _path, _body: (_ for _ in ()).throw(ConnectionError("api down")),
        event_id="evt-retry",
        force=True,
    )
    assert failed is not None and failed.status == "error"
    assert first.event_status("evt-retry")["attempt_count"] == 1

    restarted = TurnLedger(path, base_backoff_seconds=0)
    completed = restarted.process_next(
        lambda _path, _body: _response("memory-retry"),
        event_id="evt-retry",
        force=True,
    )
    assert completed is not None and completed.status == "done"
    assert restarted.event_status("evt-retry")["attempt_count"] == 2


def test_ingest_exposes_queue_and_processing_latency_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moments = iter((100.0, 102.0, 105.5, 105.5))
    last = [105.5]

    def now() -> float:
        last[0] = next(moments, last[0])
        return last[0]

    monkeypatch.setattr("turn_ingest.time.time", now)
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"), base_backoff_seconds=0)
    principal = _principal("hermes-fries", "hermes", "macmini", "credential-1")

    ledger.submit_turn(_turn("evt-latency"), principal)
    result = ledger.process_next(
        lambda _path, _body: _response("memory-latency"),
        event_id="evt-latency",
        force=True,
    )

    assert result is not None and result.status == "done"
    status = ledger.event_status("evt-latency")
    assert status["queue_seconds"] == pytest.approx(2.0)
    assert status["processing_seconds"] == pytest.approx(3.5)
    assert status["total_seconds"] == pytest.approx(5.5)

    performance = ledger.stats()["performance"]
    assert performance["completed_events"] == 1
    assert performance["queue_seconds"]["p50"] == pytest.approx(2.0)
    assert performance["processing_seconds"]["p50"] == pytest.approx(3.5)


def test_processing_latency_accumulates_across_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = [10.0]
    monkeypatch.setattr("turn_ingest.time.time", lambda: current[0])
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"), base_backoff_seconds=0)
    principal = _principal("pi-fries", "pi", "macmini", "credential-pi")
    ledger.submit_turn(_turn("evt-retry-latency"), principal)

    def fail_delivery(_path: str, _body: dict) -> dict:
        current[0] = 13.0
        raise ConnectionError("temporary")

    current[0] = 11.0
    failed = ledger.process_next(
        fail_delivery,
        event_id="evt-retry-latency",
        force=True,
    )
    assert failed is not None and failed.status == "error"

    def complete_delivery(_path: str, _body: dict) -> dict:
        current[0] = 24.0
        return _response("memory-retry-latency")

    current[0] = 20.0
    completed = ledger.process_next(
        complete_delivery,
        event_id="evt-retry-latency",
        force=True,
    )
    assert completed is not None and completed.status == "done"

    status = ledger.event_status("evt-retry-latency")
    assert status["queue_seconds"] == pytest.approx(1.0)
    assert status["processing_seconds"] == pytest.approx(6.0)
    assert status["total_seconds"] == pytest.approx(14.0)


def test_update_and_reinforcement_preserve_multiple_contributors_and_modes(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"), base_backoff_seconds=0)
    hermes = _principal("hermes-fries", "hermes", "macmini", "credential-hermes")
    claude = _principal("claude-fries", "claude_code", "macmini", "credential-claude")

    ledger.submit_turn(_turn("evt-auto"), hermes)
    ledger.process_next(
        lambda _path, _body: _response("shared-memory", "add"),
        event_id="evt-auto",
        force=True,
    )
    ledger.submit_memory(
        {
            "event_id": "evt-explicit",
            "session_id": "claude-session",
            "content": "corrected durable fact",
            "timestamp": "2026-08-05T10:01:00Z",
        },
        claude,
        capture_mode="explicit_remember",
    )
    ledger.process_next(
        lambda _path, _body: _response("shared-memory", "update", ["shared-memory"]),
        event_id="evt-explicit",
        force=True,
    )
    ledger.record_response(
        _response("shared-memory", "reinforcement", ["shared-memory"]),
        hermes,
        capture_mode="auto_hook",
        event_id="evt-reinforce",
        occurred_at=1_775_000_000,
    )

    provenance = ledger.provenance_for(["shared-memory"])["shared-memory"]
    assert provenance["origin"]["client_id"] == "hermes-fries"
    assert provenance["last_source"]["operation"] == "reinforcement"
    assert {item["client_id"] for item in provenance["contributors"]} == {
        "hermes-fries",
        "claude-fries",
    }
    modes = {mode for item in provenance["contributors"] for mode in item["capture_modes"]}
    assert modes == {"auto_hook", "explicit_remember"}


def test_merge_inherits_origins_and_contributors(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"))
    first = _principal("client-a", "hermes", "host-a", "credential-a")
    second = _principal("client-b", "claude_code", "host-b", "credential-b")
    merger = _principal("client-c", "omp", "host-c", "credential-c")

    ledger.record_response(_response("memory-a"), first, capture_mode="auto_hook", event_id="event-a", occurred_at=100)
    ledger.record_response(
        _response("memory-b"), second, capture_mode="explicit_remember", event_id="event-b", occurred_at=200
    )
    ledger.record_response(
        _response("memory-merged", "merge", ["memory-a", "memory-b"]),
        merger,
        capture_mode="auto_hook",
        event_id="event-c",
        occurred_at=300,
    )

    provenance = ledger.provenance_for(["memory-merged"])["memory-merged"]
    assert provenance["origin"]["client_id"] == "client-a"
    assert provenance["last_source"]["client_id"] == "client-c"
    assert {item["client_id"] for item in provenance["contributors"]} == {
        "client-a",
        "client-b",
        "client-c",
    }


def test_import_mode_stays_separate_from_local_panel_authority(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"))
    panel = {
        "client_id": "mm-panel-host",
        "agent_kind": "operator",
        "instance": "host",
        "credential_id": "local-panel",
        "display_name": "Panel import",
        "scope": "write",
        "authority": "local_panel",
    }
    ledger.record_response(
        _response("imported-memory"),
        panel,
        capture_mode="import",
        event_id="import-event",
        occurred_at=400,
    )

    provenance = ledger.provenance_for(["imported-memory"])["imported-memory"]
    assert provenance["origin"]["capture_mode"] == "import"
    assert provenance["origin"]["authority"] == "local_panel"
    assert provenance["contributors"][0]["capture_modes"] == ["import"]


def test_done_event_retention_does_not_delete_compact_lineage(tmp_path: Path) -> None:
    ledger = TurnLedger(
        str(tmp_path / "ledger.sqlite3"),
        done_retention_days=1,
        capture_retention_days=365,
    )
    principal = _principal("client-retention", "omp", "host", "credential-retention")
    ledger.submit_turn(_turn("retained-event"), principal)
    ledger.process_next(
        lambda _path, _body: _response("retained-memory"),
        event_id="retained-event",
        force=True,
    )
    with ledger._connect() as connection:
        connection.execute("UPDATE ingest_events SET done_at = 1 WHERE event_id = ?", ("retained-event",))

    removed = ledger.cleanup(now=200_000)
    assert removed["events"] == 1
    assert ledger.event_status("retained-event")["status"] == "missing"
    assert ledger.provenance_for(["retained-memory"])["retained-memory"]["origin"]["client_id"] == "client-retention"


def test_rotated_credentials_remain_one_stable_contributor(tmp_path: Path) -> None:
    ledger = TurnLedger(str(tmp_path / "ledger.sqlite3"))
    old_key = _principal("stable-client", "hermes", "host", "credential-old")
    new_key = _principal("stable-client", "hermes", "host", "credential-new")
    ledger.record_response(
        _response("rotated-memory"),
        old_key,
        capture_mode="auto_hook",
        event_id="event-old",
        occurred_at=100,
    )
    ledger.record_response(
        _response("rotated-memory", "update", ["rotated-memory"]),
        new_key,
        capture_mode="auto_hook",
        event_id="event-new",
        occurred_at=200,
    )

    provenance = ledger.provenance_for(["rotated-memory"])["rotated-memory"]
    assert len(provenance["contributors"]) == 1
    assert provenance["origin"]["credential_id"] == "credential-old"
    assert provenance["last_source"]["credential_id"] == "credential-new"
