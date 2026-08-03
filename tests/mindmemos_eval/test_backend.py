from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from mindmemos_eval.backend import (
    MindMemOSBackend,
    adapt_project_override_config_for_lite,
    build_mindmemos_backend,
)
from mindmemos_eval.memory import runner as memory_runner
from mindmemos_eval.skills.args import add_skill_evolution_args
from mindmemos_eval.skills.evolve import MindMemOSSkillEvolutionClient
from mindmemos_eval.skills.runners import _build_evolver
from mindmemos_sdk.config import HttpConnectionConfig, InMemoryConnectionConfig
from mindmemos_sdk.skills import serialize_bundle


class _FakeRootClient:
    def __init__(self, *, config, config_manager=None):
        self.config = config
        self.config_manager = config_manager
        self.memory = object()
        self.skills = object()
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1
        return self

    async def aclose(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_shared_backend_builds_http_and_in_memory_sdk_connections(monkeypatch) -> None:
    from mindmemos_eval import backend as backend_module

    monkeypatch.setattr(backend_module, "AsyncMindMemOSClient", _FakeRootClient)

    http = build_mindmemos_backend(
        connection_mode="http",
        base_url="https://api.test",
        api_key="mk-test",
        project_id="ignored-by-http",
        timeout_seconds=90,
    )
    http_config = http.client.config.connections["eval"]
    assert isinstance(http_config, HttpConnectionConfig)
    assert http_config.base_url == "https://api.test"
    assert http_config.api_key == "mk-test"
    assert http_config.timeout_seconds == 90

    embedded = build_mindmemos_backend(
        connection_mode="in_memory",
        project_id="project-1",
        lite_config_path="config/lite.yaml",
        lite_start_workers=False,
        project_override_config={"algo_config": {"add": {"enable_entities": False}}},
    )
    embedded_config = embedded.client.config.connections["eval"]
    assert isinstance(embedded_config, InMemoryConnectionConfig)
    assert embedded_config.runtime == "mindmemos_lite"
    assert embedded_config.project_id == "project-1"
    assert embedded_config.config_path == Path("config/lite.yaml")
    assert embedded_config.start_workers is False
    assert embedded_config.project_override_config == {
        "algo_config": {"add": {"enable_entities": False}}
    }

    assert http.client.config.clients.memory.connection == "eval"
    assert http.client.config.clients.skills.connection == "eval"
    await http.start()
    await http.start()
    await http.aclose()
    assert http.client.started == 1
    assert http.client.closed == 1


def test_memory_builder_uses_shared_backend(monkeypatch) -> None:
    class Backend:
        memory = object()
        started = 0

        async def start(self):
            self.started += 1

    backend = Backend()
    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return backend

    monkeypatch.setattr(memory_runner, "build_mindmemos_backend", fake_build)
    owner = memory_runner._build_memory_client(
        "https://api.test",
        "mk-test",
        45,
        connection_mode="in_memory",
        project_id="project-1",
        lite_config_name="test",
        project_override_config={
            "algo_config": {
                "common": {"prompt_language": "EN"},
                "add": {
                    "vanilla": {
                        "chunk_soft_token_budget": 26000,
                        "enable_entities": False,
                    }
                },
                "search": {
                    "request_top_k_max": 100,
                    "vanilla": {
                        "recall_size": 50,
                        "use_reranker": True,
                    },
                },
            }
        },
    )

    assert owner is backend
    assert backend.started == 0
    assert captured["connection_mode"] == "in_memory"
    assert captured["project_id"] == "project-1"
    assert captured["lite_config_name"] == "test"
    assert captured["project_override_config"] == {
        "algo_config": {
            "add": {
                "chunker": {"chunk_soft_token_budget": 26000},
                "enable_entities": False,
            },
            "search": {
                "recall_size": 50,
                "use_reranker": True,
            },
        }
    }


@pytest.mark.asyncio
async def test_in_memory_matrix_needs_no_api_key_file_or_external_reset(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "matrix.yaml"
    config_path.write_text(
        """
benchmarks:
  locomo:
    dataset: data/locomo.json
    memory_algorithm: vanilla
""".lstrip(),
        encoding="utf-8",
    )
    reset_calls = []

    async def reset(*args):
        reset_calls.append(args)

    monkeypatch.setattr(memory_runner, "reset_project", reset)

    class Adapter:
        async def run(self, **kwargs):
            assert kwargs["memory"]._inner is memory
            return {"ok": True}

    memory = object()

    class Backend:
        def __init__(self):
            self.memory = memory

        async def start(self):
            return self

        async def aclose(self):
            return None

    async def factory(_identity):
        return Backend()

    manifests = await memory_runner.run_benchmark_matrix(
        SimpleNamespace(
            benchmark_config=str(config_path),
            benchmark_list="locomo",
            manifest_output=str(tmp_path / "manifest.jsonl"),
            api_key_output=None,
            reuse_api_key=None,
            memory_connection_mode="in_memory",
            add=True,
            skip_clean=False,
        ),
        adapters={"locomo": Adapter()},
        memory_client_factory=factory,
        answer_llm_factory=lambda: object(),
        judge_llm_factory=lambda: object(),
    )

    assert reset_calls == []
    assert manifests[0].api_key_file == ""


@pytest.mark.asyncio
async def test_in_memory_matrix_forwards_project_override_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "matrix.yaml"
    config_path.write_text(
        """
benchmarks:
  locomo:
    dataset: data/locomo.json
    default_algorithm: vanilla
algorithm_profiles:
  vanilla:
    memory_algorithm: vanilla
    project_override_config:
      algo_config:
        add:
          vanilla:
            enable_entities: false
""".lstrip(),
        encoding="utf-8",
    )
    captured = {}
    memory = object()

    class Backend:
        async def start(self):
            return self

        async def aclose(self):
            return None

    backend = Backend()
    backend.memory = memory

    def fake_build_backend(**kwargs):
        captured.update(kwargs)
        return backend

    monkeypatch.setattr(memory_runner, "build_mindmemos_backend", fake_build_backend)

    class Adapter:
        async def run(self, **kwargs):
            assert kwargs["memory"]._inner is memory
            return {"ok": True}

    await memory_runner.run_benchmark_matrix(
        SimpleNamespace(
            benchmark_config=str(config_path),
            benchmark_list="locomo",
            manifest_output=str(tmp_path / "manifest.jsonl"),
            auth_config_output=None,
            reuse_api_key=None,
            memory_connection_mode="in_memory",
            add=True,
            skip_clean=False,
        ),
        adapters={"locomo": Adapter()},
        answer_llm_factory=lambda: object(),
        judge_llm_factory=lambda: object(),
    )

    assert captured["project_override_config"] == {
        "algo_config": {"add": {"enable_entities": False}}
    }


def test_personamem_vanilla_project_override_adapts_to_lite_config() -> None:
    from mindmemos_eval.memory.config import load_benchmark_specs

    path = Path("config/mindmemos_eval/memory_evaluation_personamem.example.yaml")
    source = load_benchmark_specs(path, algorithm_override="vanilla")["personamem"].project_override_config

    adapted = adapt_project_override_config_for_lite(source, memory_algorithm="vanilla")

    assert source is not None
    assert adapted is not None
    source_add = source["algo_config"]["add"]["vanilla"]
    source_search = source["algo_config"]["search"]
    algo = adapted["algo_config"]
    assert algo["add"]["enable_entities"] is False
    assert algo["add"]["chunker"] == {
        name: value for name, value in source_add.items() if name != "enable_entities"
    }
    assert source_search["request_top_k_max"] == 100
    assert algo["search"] == source_search["vanilla"]


def test_lite_project_override_adapter_rejects_non_equivalent_limits() -> None:
    with pytest.raises(ValueError, match="cannot preserve request_top_k_max=50"):
        adapt_project_override_config_for_lite(
            {
                "algo_config": {
                    "search": {
                        "request_top_k_max": 50,
                        "vanilla": {},
                    }
                }
            },
            memory_algorithm="vanilla",
        )


def test_evolver_builder_uses_shared_backend_for_lite(monkeypatch) -> None:
    from mindmemos_eval.skills import runners

    backend = SimpleNamespace()
    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return backend

    monkeypatch.setattr(runners, "build_mindmemos_backend", fake_build)
    evolver = _build_evolver(
        SimpleNamespace(
            evolve=True,
            evolution_connection_mode="in_memory",
            evolution_base_url=None,
            evolution_api_key=None,
            evolution_timeout_seconds=321.0,
            evolution_project_id="skill-project",
            evolution_lite_config_path="config/lite.yaml",
            evolution_lite_config_name="dev",
            evolution_lite_load_config_from_env=False,
            evolution_lite_start_workers=True,
        ),
        "spreadsheetbench",
    )

    assert isinstance(evolver, MindMemOSSkillEvolutionClient)
    assert captured["connection_mode"] == "in_memory"
    assert captured["project_id"] == "skill-project"
    assert captured["timeout_seconds"] == 321.0
    assert captured["user_id"] == "spreadsheetbench-eval"


def test_skill_evolution_timeout_is_configurable_from_cli() -> None:
    parser = argparse.ArgumentParser()
    add_skill_evolution_args(parser)

    args = parser.parse_args(["--evolution-timeout-seconds", "45.5"])

    assert args.evolution_timeout_seconds == 45.5


@pytest.mark.asyncio
async def test_backend_neutral_evolver_uses_memory_and_skill_resources(tmp_path: Path) -> None:
    calls = []

    class Skills:
        async def register(self, *, name, content):
            calls.append(("register", name, content))
            return SimpleNamespace(cloud_skill_id="cloud-1", version_id="v1", content_hash="hash-v1")

        async def evolve(self, cloud_skill_id, *, mode):
            calls.append(("evolve", cloud_skill_id, mode))
            return SimpleNamespace(
                evolved=True,
                pending_count=1,
                threshold=1,
                new_version_id="v2",
                new_version_ids=["v2"],
                summarized_count=1,
                consumed_count=1,
            )

        async def get_content(self, cloud_skill_id, version_id):
            calls.append(("content", cloud_skill_id, version_id))
            return SimpleNamespace(
                content=serialize_bundle({"SKILL.md": "evolved"}),
                version=SimpleNamespace(content_hash="hash-v2"),
            )

    class Memory:
        async def add(self, messages, **kwargs):
            calls.append(("add", messages, kwargs))

    class Root:
        memory = Memory()
        skills = Skills()
        started = 0
        closed = 0

        async def start(self):
            self.started += 1

        async def aclose(self):
            self.closed += 1

    root = Root()
    evolver = MindMemOSSkillEvolutionClient(
        MindMemOSBackend(root),
        transcript_metadata={"benchmark": "sheet"},
    )
    skill_dir = tmp_path / "spreadsheet"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("original", encoding="utf-8")

    await evolver.prepare([skill_dir])
    await evolver.record_case(
        SimpleNamespace(
            case_id="case-1",
            score=1.0,
            messages=[{"role": "user", "content": "do it"}],
        )
    )
    outcomes = await evolver.evolve()
    await evolver.aclose()

    add_call = next(call for call in calls if call[0] == "add")
    assert add_call[2]["skill_context"][0].base_version_id == "v1"
    assert add_call[2]["metadata"] == {"benchmark": "sheet", "case_id": "case-1"}
    assert outcomes[0].new_version_id == "v2"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "evolved"
    assert root.started == 1
    assert root.closed == 1
