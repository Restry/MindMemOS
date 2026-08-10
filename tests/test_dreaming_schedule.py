from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_local_dreaming.py"
PLIST = ROOT / "panel/deploy/macos/com.leway.mindmemos.dreaming.plist"
LINUX_SERVICE = ROOT / "panel/deploy/systemd/mindmemos-company-dreaming.service"


def _load_runner():
    spec = importlib.util.spec_from_file_location("test_local_dreaming_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_dreaming_is_bounded_in_runtime_config() -> None:
    config = yaml.safe_load((ROOT / "config/mindmemos/dev.yaml").read_text(encoding="utf-8"))
    dreaming = config["algo_config"]["dreaming"]

    assert dreaming["max_scopes_per_run"] == 10
    assert dreaming["max_seed_memories"] == 500
    assert dreaming["max_memories_per_scope"] == 20
    assert dreaming["concurrency"] == 2


def test_local_dreaming_runner_uses_sync_user_scoped_request() -> None:
    runner = _load_runner()

    assert runner.build_request("leway") == {"mode": "sync", "user_id": "leway"}


def test_dreaming_runner_rejects_missing_user_id() -> None:
    runner = _load_runner()

    with pytest.raises(RuntimeError, match="MINDMEMOS_DREAMING_USER"):
        runner.require_user_id("")


def test_local_dreaming_launch_agent_runs_daily_at_0430() -> None:
    config = plistlib.loads(PLIST.read_bytes())

    assert config["Label"] == "com.leway.mindmemos.dreaming"
    assert config["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert config["ProgramArguments"] == [
        "/Users/leway/Projects/MindMemOS/.venv/bin/python",
        "/Users/leway/Projects/MindMemOS/scripts/run_local_dreaming.py",
    ]
    assert config["EnvironmentVariables"]["MM_RECALL_JUDGE_ENABLED"] == "0"
    assert config["EnvironmentVariables"]["MINDMEMOS_DREAMING_USER"] == "leway"


def test_company_systemd_service_sets_explicit_company_user() -> None:
    text = LINUX_SERVICE.read_text(encoding="utf-8")

    assert "Environment=MINDMEMOS_DREAMING_USER=company" in text
