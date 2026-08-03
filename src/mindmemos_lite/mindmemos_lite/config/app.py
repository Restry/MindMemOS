import logging
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .base import build
from .database import (
    DatabaseBackendConfig,
    DatabaseBackendRequirementsConfig,
    DatabaseConfig,
    PgVectorConfig,
)
from .memory import MemoryConfig
from .model import ModelEndpointConfig, ModelRouterConfig
from .validation import validate_tree

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_ROOT = REPO_ROOT / "config"
DEFAULT_MINDMEMOS_CONFIG_ROOT = DEFAULT_CONFIG_ROOT / "mindmemos_lite"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONFIG_ROOT",
    "DEFAULT_ENV_PATH",
    "DEFAULT_MINDMEMOS_CONFIG_ROOT",
    "REPO_ROOT",
    "DatabaseBackendConfig",
    "DatabaseBackendRequirementsConfig",
    "DatabaseConfig",
    "MemoryConfig",
    "ModelEndpointConfig",
    "ModelRouterConfig",
    "PgVectorConfig",
    "build_config",
    "default_config_path",
]


def default_config_path(config_name: str) -> Path:
    if config_name == "dev":
        return DEFAULT_MINDMEMOS_CONFIG_ROOT / "dev.yaml"
    return DEFAULT_CONFIG_ROOT / f"{config_name}.yaml"


def build_config(config_name: str = "dev", config_path: str | Path | None = None) -> MemoryConfig:
    load_dotenv(DEFAULT_ENV_PATH, override=False)
    resolved_path = default_config_path(config_name) if config_path is None else Path(config_path)
    resolved_path = resolved_path.expanduser().resolve()
    logger.info("loading config from %s", resolved_path)

    with resolved_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    cfg = build(MemoryConfig, data)
    validate_tree(cfg)
    return cfg
