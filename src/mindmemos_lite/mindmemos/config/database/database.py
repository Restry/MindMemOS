"""Database configuration composition."""

from dataclasses import dataclass, field

from ...errors import InvalidConfigError
from ..base import MindMemOSConfig
from ..validation import join_path
from .backend import DatabaseBackendConfig
from .pgvector import PgVectorConfig


@dataclass
class DatabaseConfig(MindMemOSConfig):
    """Database backend selection and provider-specific configuration."""

    backend: DatabaseBackendConfig = field(default_factory=DatabaseBackendConfig)
    pgvector: PgVectorConfig = field(default_factory=PgVectorConfig)
    default_consistency: str = "fast"

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        if value.default_consistency not in {"fast", "strong"}:
            raise InvalidConfigError(
                join_path(path, "default_consistency"),
                support="fast or strong",
            )
