"""Shared MindMemOS backend composition for evaluation runners."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, Self

from mindmemos_sdk.config import (
    ConfigManager,
    DefaultsConfig,
    HttpConnectionConfig,
    InMemoryConnectionConfig,
    SDKConfig,
)

from mindmemos_sdk import AsyncMindMemOSClient

ConnectionMode = Literal["http", "in_memory"]

_VANILLA_CHUNKER_FIELDS = frozenset(
    {
        "chunk_soft_token_budget",
        "chunk_hard_token_budget",
        "turn_hard_token_budget",
        "history_soft_token_budget",
        "history_hard_token_budget",
        "history_min_turn_count",
        "compaction_soft_token_budget",
        "compaction_head_tokens",
        "compaction_tail_tokens",
        "compaction_summary_context_token_budget",
        "compaction_summary_output_token_budget",
        "time_gap_threshold_seconds",
        "template_tokens",
        "recall_budget",
        "output_headroom",
    }
)


def adapt_project_override_config_for_lite(
    project_config: dict[str, Any] | None,
    *,
    memory_algorithm: str,
) -> dict[str, Any] | None:
    """Translate the main-service vanilla override tree to Lite's native shape."""

    if not project_config:
        return None
    config = deepcopy(project_config)
    raw_algo = config.get("algo_config")
    if raw_algo is None:
        return config
    if not isinstance(raw_algo, Mapping):
        raise ValueError("project_override_config.algo_config must be a mapping")

    raw_add = raw_algo.get("add")
    raw_search = raw_algo.get("search")
    uses_main_shape = (
        "common" in raw_algo
        or (isinstance(raw_add, Mapping) and "vanilla" in raw_add)
        or (
            isinstance(raw_search, Mapping)
            and ("vanilla" in raw_search or "request_top_k_max" in raw_search)
        )
    )
    if not uses_main_shape:
        return config
    if memory_algorithm != "vanilla":
        raise ValueError("MindMemOS Lite project override adaptation currently supports only vanilla")

    unknown_algo_sections = set(raw_algo) - {"common", "add", "search"}
    if unknown_algo_sections:
        names = ", ".join(sorted(unknown_algo_sections))
        raise ValueError(f"unsupported Lite project override algo_config section(s): {names}")

    common = raw_algo.get("common") or {}
    if not isinstance(common, Mapping):
        raise ValueError("project_override_config.algo_config.common must be a mapping")
    unknown_common = set(common) - {"prompt_language"}
    if unknown_common:
        names = ", ".join(sorted(unknown_common))
        raise ValueError(f"unsupported Lite common project override field(s): {names}")
    prompt_language = str(common.get("prompt_language") or "EN").upper()
    if prompt_language != "EN":
        raise ValueError(
            "MindMemOS Lite has automatic language detection with an EN fallback; "
            f"cannot preserve prompt_language={prompt_language!r}"
        )

    lite_algo: dict[str, Any] = {}
    if raw_add:
        if not isinstance(raw_add, Mapping):
            raise ValueError("project_override_config.algo_config.add must be a mapping")
        unknown_add_sections = set(raw_add) - {"vanilla"}
        if unknown_add_sections:
            names = ", ".join(sorted(unknown_add_sections))
            raise ValueError(f"unsupported Lite add project override section(s): {names}")
        vanilla_add = raw_add.get("vanilla") or {}
        if not isinstance(vanilla_add, Mapping):
            raise ValueError("project_override_config.algo_config.add.vanilla must be a mapping")
        unknown_add_fields = set(vanilla_add) - _VANILLA_CHUNKER_FIELDS - {
            "enable_entities",
            "recall",
            "safety_gate",
        }
        if unknown_add_fields:
            names = ", ".join(sorted(unknown_add_fields))
            raise ValueError(f"unsupported Lite vanilla add project override field(s): {names}")

        lite_add: dict[str, Any] = {}
        chunker = {name: deepcopy(vanilla_add[name]) for name in _VANILLA_CHUNKER_FIELDS if name in vanilla_add}
        if chunker:
            lite_add["chunker"] = chunker
        for name in ("enable_entities", "recall", "safety_gate"):
            if name in vanilla_add:
                lite_add[name] = deepcopy(vanilla_add[name])
        if lite_add:
            lite_algo["add"] = lite_add

    if raw_search:
        if not isinstance(raw_search, Mapping):
            raise ValueError("project_override_config.algo_config.search must be a mapping")
        unknown_search_sections = set(raw_search) - {"request_top_k_max", "vanilla"}
        if unknown_search_sections:
            names = ", ".join(sorted(unknown_search_sections))
            raise ValueError(f"unsupported Lite search project override section(s): {names}")
        request_top_k_max = int(raw_search.get("request_top_k_max", 100))
        if request_top_k_max != 100:
            raise ValueError(
                "MindMemOS Lite fixes the vanilla request/recall ceiling at 100; "
                f"cannot preserve request_top_k_max={request_top_k_max}"
            )
        vanilla_search = raw_search.get("vanilla") or {}
        if not isinstance(vanilla_search, Mapping):
            raise ValueError("project_override_config.algo_config.search.vanilla must be a mapping")
        if vanilla_search:
            lite_algo["search"] = deepcopy(dict(vanilla_search))

    if lite_algo:
        config["algo_config"] = lite_algo
    else:
        config.pop("algo_config", None)
    return config


class MindMemOSBackend:
    """Own one SDK client shared by Memory and Skill evaluation resources."""

    def __init__(self, client: AsyncMindMemOSClient) -> None:
        self.client = client
        self._started = False

    @property
    def memory(self):
        """Return the SDK Memory resource client."""

        return self.client.memory

    @property
    def skills(self):
        """Return the SDK Skill resource client."""

        return self.client.skills

    async def start(self) -> Self:
        """Open the configured HTTP or in-memory connection once."""

        if not self._started:
            await self.client.start()
            self._started = True
        return self

    async def aclose(self) -> None:
        """Close the shared connection and any owned Lite runtime."""

        await self.client.aclose()
        self._started = False

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def build_mindmemos_backend(
    *,
    connection_mode: ConnectionMode,
    project_id: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: float = 30.0,
    max_retries: int = 2,
    lite_config_path: str | Path | None = None,
    lite_config_name: str = "dev",
    lite_load_config_from_env: bool = False,
    lite_start_workers: bool = True,
    project_override_config: dict[str, Any] | None = None,
    account_id: str = "eval",
    api_key_uuid: str = "eval-sdk",
    user_id: str | None = None,
    app_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    config_manager: ConfigManager | None = None,
) -> MindMemOSBackend:
    """Build one backend over HTTP or an owned MindMemOS Lite runtime.

    HTTP uses the public MindMemOS API contract and can target either the main
    service or a Lite FastAPI service. In-memory mode is intentionally explicit:
    the SDK currently supports only ``mindmemos_lite`` as an embedded runtime.
    """

    if connection_mode == "http":
        if not base_url:
            raise ValueError("base_url is required for an HTTP MindMemOS backend")
        connection = HttpConnectionConfig(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
    elif connection_mode == "in_memory":
        if not project_id:
            raise ValueError("project_id is required for an in-memory MindMemOS backend")
        connection = InMemoryConnectionConfig(
            runtime="mindmemos_lite",
            project_id=project_id,
            config_path=Path(lite_config_path) if lite_config_path is not None else None,
            config_name=lite_config_name,
            load_config_from_env=lite_load_config_from_env,
            start_workers=lite_start_workers,
            account_id=account_id,
            api_key_uuid=api_key_uuid,
            project_override_config=project_override_config,
        )
    else:
        raise ValueError(f"unsupported MindMemOS connection mode: {connection_mode!r}")

    connection_name = "eval"
    config = SDKConfig(
        defaults=DefaultsConfig(
            user_id=user_id,
            app_id=app_id,
            agent_id=agent_id,
            session_id=session_id,
        ),
        connections={connection_name: connection},
        clients={
            "memory": {"connection": connection_name},
            "skills": {"connection": connection_name},
        },
    )
    return MindMemOSBackend(
        AsyncMindMemOSClient(
            config=config,
            config_manager=config_manager,
        )
    )


__all__ = [
    "ConnectionMode",
    "MindMemOSBackend",
    "adapt_project_override_config_for_lite",
    "build_mindmemos_backend",
]
