"""Top-level MindMemOS Lite process configuration."""

from dataclasses import dataclass, field

from ..errors import InvalidConfigError
from .base import MindMemOSConfig, frozen_field
from .database import DatabaseConfig
from .model import ModelRouterConfig
from .observability import ObservabilityConfig
from .pipelines import PipelineRoutingConfig
from .validation import join_path
from .vanilla import VanillaAlgorithmConfig


@dataclass
class MemoryConfig(MindMemOSConfig):
    """Compose the model, database, and algorithm configuration subtrees."""

    observability: ObservabilityConfig = frozen_field(default_factory=ObservabilityConfig)
    chat_model_router: ModelRouterConfig = field(default_factory=ModelRouterConfig)
    embed_model_router: ModelRouterConfig = field(default_factory=ModelRouterConfig)
    rerank_model_router: ModelRouterConfig = field(default_factory=ModelRouterConfig)
    database: DatabaseConfig = frozen_field(default_factory=DatabaseConfig)
    pipelines: PipelineRoutingConfig = field(default_factory=PipelineRoutingConfig)
    algo_config: VanillaAlgorithmConfig = field(default_factory=VanillaAlgorithmConfig)

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        dimensions = {
            endpoint.dimensions for endpoint in value.embed_model_router.endpoints if endpoint.dimensions is not None
        }
        if len(dimensions) > 1:
            raise InvalidConfigError(
                join_path(path, "embed_model_router.endpoints[].dimensions"),
                support="one consistent embedding dimension across all endpoints",
            )
