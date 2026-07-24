from typing import Any

from pydantic import BaseModel, Field


class Reward(BaseModel):
    score: float
    """奖励分数"""

    detail: str | None = None
    """测评详情"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """测评metadata"""
