"""Configuration for one LiteLLM-compatible model endpoint."""

from dataclasses import dataclass, field

from ..base import MindMemOSConfig, secret_field
from ..validation import (
    join_path,
    non_negative_integer,
    non_negative_optional,
    positive_number,
    positive_optional,
    range_optional,
    require_string,
)


@dataclass
class ModelEndpointConfig(MindMemOSConfig):
    """One LiteLLM-compatible model endpoint."""

    model: str
    api_base: str
    api_key: str = secret_field()
    rpm: int | None = None
    tpm: int | None = None
    timeout: int = 600
    num_retries: int = 50
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    encoding_format: str | None = None
    dimensions: int | None = None
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        require_string(join_path(path, "model"), value.model)
        require_string(join_path(path, "api_key"), value.api_key)
        positive_optional(join_path(path, "rpm"), value.rpm)
        positive_optional(join_path(path, "tpm"), value.tpm)
        positive_number(join_path(path, "timeout"), value.timeout)
        non_negative_integer(join_path(path, "num_retries"), value.num_retries)
        non_negative_optional(join_path(path, "temperature"), value.temperature)
        range_optional(join_path(path, "top_p"), value.top_p, minimum=0, maximum=1)
        positive_optional(join_path(path, "max_tokens"), value.max_tokens)
        positive_optional(join_path(path, "max_completion_tokens"), value.max_completion_tokens)
        positive_optional(join_path(path, "dimensions"), value.dimensions)
