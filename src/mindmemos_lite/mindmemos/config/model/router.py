"""Configuration for model endpoint routing."""

from dataclasses import dataclass, field

from ..base import MindMemOSConfig
from ..validation import join_path, non_negative_optional, positive_integer, require_string
from .endpoint import ModelEndpointConfig


@dataclass
class ModelRouterConfig(MindMemOSConfig):
    """A group of interchangeable endpoints exposed through one model alias."""

    endpoints: list[ModelEndpointConfig] = field(default_factory=list)
    routing_strategy: str = "simple-shuffle"
    allowed_fails: int | None = None
    cool_down: int | float | None = None
    format_parser_max_attempts: int = 3
    dimensions_supported_models: list[str] = field(default_factory=list)

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        require_string(join_path(path, "routing_strategy"), value.routing_strategy)
        non_negative_optional(join_path(path, "allowed_fails"), value.allowed_fails)
        non_negative_optional(join_path(path, "cool_down"), value.cool_down)
        positive_integer(join_path(path, "format_parser_max_attempts"), value.format_parser_max_attempts)
        for index, model_prefix in enumerate(value.dimensions_supported_models):
            field_path = f"{join_path(path, 'dimensions_supported_models')}[{index}]"
            require_string(field_path, model_prefix)
