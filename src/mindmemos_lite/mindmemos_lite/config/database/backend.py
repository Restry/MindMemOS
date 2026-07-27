"""Backend selection and capability requirements."""

from dataclasses import dataclass, field

from ...errors import InvalidConfigError
from ..base import MindMemOSConfig
from ..validation import join_path, positive_optional, require_string


@dataclass
class DatabaseBackendRequirementsConfig(MindMemOSConfig):
    """Portable capabilities required from the selected database backend."""

    dense_vector: bool = True
    sparse_vector: bool = True
    hybrid_search: bool = True
    metadata_filtering: bool = True
    batch_record_io: bool = True
    atomic_batch_write: bool = False
    max_vector_dimensions: int | None = None

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        positive_optional(join_path(path, "max_vector_dimensions"), value.max_vector_dimensions)


@dataclass
class DatabaseBackendConfig(MindMemOSConfig):
    """Select the backend used by the vector database service."""

    provider: str = "pgvector"
    graph_enabled: bool = True
    required: DatabaseBackendRequirementsConfig = field(default_factory=DatabaseBackendRequirementsConfig)

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        require_string(join_path(path, "provider"), value.provider)
        if value.provider.strip().lower() != "pgvector":
            raise InvalidConfigError(
                join_path(path, "provider"),
                support="pgvector",
            )
