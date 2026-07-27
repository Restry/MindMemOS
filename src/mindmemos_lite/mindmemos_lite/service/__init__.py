"""Application service contracts for MindMemOS Lite."""

from typing import Any

from .ports import (
    InternalMemoryPort,
    InternalMemoryService,
    MemoryPort,
    MemoryService,
    SkillPort,
    SkillService,
)

__all__ = [
    "BaseMemoryService",
    "InternalMemoryPort",
    "InternalMemoryService",
    "MemoryPort",
    "MemoryService",
    "SkillPort",
    "SkillService",
]


def __getattr__(name: str) -> Any:
    if name == "BaseMemoryService":
        from .base import BaseMemoryService

        return BaseMemoryService
    raise AttributeError(name)
