from __future__ import annotations

from dataclasses import fields, is_dataclass
from numbers import Real
from typing import Any

from omegaconf import DictConfig, ListConfig, OmegaConf

from ..errors import InvalidConfigError, MissingConfigValueError
from .base import MindMemOSConfig


def validate_tree(value: Any, path: str = "") -> None:
    """Recursively validate children first, then the current config node."""

    schema: type | None = None

    if isinstance(value, DictConfig):
        for key in value.keys():
            validate_tree(value[key], join_path(path, str(key)))
        schema = OmegaConf.get_type(value)
    elif isinstance(value, ListConfig):
        for index, item in enumerate(value):
            validate_tree(item, f"{path}[{index}]")
    elif is_dataclass(value) and not isinstance(value, type):
        for dataclass_field in fields(value):
            child = getattr(value, dataclass_field.name)
            validate_tree(child, join_path(path, dataclass_field.name))
        schema = type(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_tree(item, join_path(path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_tree(item, f"{path}[{index}]")

    if isinstance(schema, type) and issubclass(schema, MindMemOSConfig):
        schema.validate_self(value, path)


def validate_config(cfg: Any) -> None:
    """Compatibility entry point for validating a complete config tree."""

    validate_tree(cfg)


def join_path(path: str, field: str) -> str:
    return f"{path}.{field}" if path else field


def require_string(path: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MissingConfigValueError(path)


def positive_optional(path: str, value: Any) -> None:
    if value is not None:
        positive_number(path, value)


def positive_number(path: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or value <= 0:
        raise InvalidConfigError(path, support="positive number")


def positive_integer(path: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidConfigError(path, support="positive integer")


def non_negative_optional(path: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Real) or value < 0:
        raise InvalidConfigError(path, support="non-negative number")


def non_negative_integer(path: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidConfigError(path, support="non-negative integer")


def range_optional(path: str, value: Any, *, minimum: float, maximum: float) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Real) or not minimum <= value <= maximum:
        raise InvalidConfigError(path, support=f"{minimum} <= value <= {maximum}")
