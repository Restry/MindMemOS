"""PgVector provider configuration."""

from dataclasses import dataclass

from ...errors import InvalidConfigError
from ..base import MindMemOSConfig, secret_field
from ..validation import (
    join_path,
    non_negative_integer,
    non_negative_optional,
    positive_integer,
    positive_number,
    require_string,
)


@dataclass
class PgVectorConfig(MindMemOSConfig):
    """PostgreSQL connection, schema, pool, and generic hybrid-search fallbacks.

    Vanilla supplies explicit dense/sparse prefetch limits from its algorithm
    config, so ``hybrid_prefetch_factor`` applies only to callers that omit
    those channel limits.
    """

    dsn: str = secret_field(default="")
    schema: str = "mindmemos"
    min_pool_size: int = 1
    max_pool_size: int = 10
    pool_timeout: float = 30.0
    create_extension: bool = True
    create_schema: bool = True
    hybrid_prefetch_factor: int = 4
    rrf_k: int = 2
    dense_weight: float = 1.0
    sparse_weight: float = 1.0

    @classmethod
    def validate_self(cls, value, path: str) -> None:
        require_string(join_path(path, "dsn"), value.dsn)
        require_string(join_path(path, "schema"), value.schema)
        non_negative_integer(join_path(path, "min_pool_size"), value.min_pool_size)
        positive_integer(join_path(path, "max_pool_size"), value.max_pool_size)
        if value.min_pool_size > value.max_pool_size:
            raise InvalidConfigError(
                join_path(path, "min_pool_size"),
                support="less than or equal to database.pgvector.max_pool_size",
            )
        positive_number(join_path(path, "pool_timeout"), value.pool_timeout)
        positive_integer(
            join_path(path, "hybrid_prefetch_factor"),
            value.hybrid_prefetch_factor,
        )
        positive_integer(join_path(path, "rrf_k"), value.rrf_k)
        non_negative_optional(join_path(path, "dense_weight"), value.dense_weight)
        non_negative_optional(join_path(path, "sparse_weight"), value.sparse_weight)
        if value.dense_weight == value.sparse_weight == 0:
            raise InvalidConfigError(
                join_path(path, "dense_weight"),
                support="at least one positive dense or sparse RRF weight",
            )
