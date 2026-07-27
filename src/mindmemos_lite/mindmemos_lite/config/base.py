from dataclasses import field, fields, is_dataclass
from typing import Any, TypeVar, cast, get_args, get_origin

from omegaconf import DictConfig, OmegaConf

T = TypeVar("T")
FROZEN_KEY = "frozen"
FROZEN_VALUE = True
SECRET_KEY = "secret"
SECRET_VALUE = True
MASK = "*****"


class MindMemOSConfig:
    """Base class for recursively validated MindMemOS config schemas."""

    @classmethod
    def validate_self(cls, value: Any, path: str) -> None:
        """Validate this node after all nested config nodes are valid."""


def frozen_field(default: Any = ..., *, secret: bool = False, **kwargs) -> Any:
    return _make_field(default, frozen=FROZEN_VALUE, secret=secret, **kwargs)


def secret_field(default: Any = ..., *, frozen: bool = False, **kwargs) -> Any:
    return _make_field(default, frozen=frozen, secret=SECRET_VALUE, **kwargs)


def _make_field(default: Any, *, frozen: bool = False, secret: bool = False, **kwargs) -> Any:
    metadata = dict(kwargs.pop("metadata", {}))
    if frozen:
        metadata[FROZEN_KEY] = FROZEN_VALUE
    if secret:
        metadata[SECRET_KEY] = SECRET_VALUE
    if default is ...:
        return field(metadata=metadata, **kwargs)
    if callable(default) and not isinstance(default, type):
        return field(default_factory=default, metadata=metadata, **kwargs)
    return field(default=default, metadata=metadata, **kwargs)


def build(schema: type[T], overrides: Any = None) -> T:
    cfg = OmegaConf.structured(schema)
    if overrides:
        cfg = OmegaConf.merge(cfg, OmegaConf.create(overrides))
    _apply_frozen_flags(cfg, schema)
    return cast("T", cfg)


def safe_dict(cfg: Any, schema: type | None = None) -> dict[str, Any]:
    if schema is None:
        schema = OmegaConf.get_type(cfg)
    raw = OmegaConf.to_container(cfg, resolve=True)
    _mask_in_place(raw, schema)
    return cast("dict[str, Any]", raw)


def _apply_frozen_flags(cfg: DictConfig, schema: type) -> None:
    if not is_dataclass(schema):
        return
    for dataclass_field in fields(schema):
        if dataclass_field.metadata.get(FROZEN_KEY):
            cfg._get_node(dataclass_field.name)._set_flag("readonly", FROZEN_VALUE)
        if is_dataclass(dataclass_field.type):
            _apply_frozen_flags(getattr(cfg, dataclass_field.name), dataclass_field.type)


def _mask_in_place(data: Any, schema: type | None) -> None:
    if not (is_dataclass(schema) and isinstance(data, dict)):
        return
    for dataclass_field in fields(schema):
        if dataclass_field.name not in data:
            continue
        value = data[dataclass_field.name]
        if dataclass_field.metadata.get(SECRET_KEY) and value is not None:
            data[dataclass_field.name] = MASK
            continue

        field_type = dataclass_field.type
        if is_dataclass(field_type):
            _mask_in_place(value, field_type)
            continue

        if get_origin(field_type) is list and isinstance(value, list):
            item_types = get_args(field_type)
            item_type = item_types[0] if item_types else None
            if is_dataclass(item_type):
                for item in value:
                    _mask_in_place(item, item_type)
