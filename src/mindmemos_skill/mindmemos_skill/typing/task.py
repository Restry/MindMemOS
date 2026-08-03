"""Agent task contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .skill import Skill


class Task(BaseModel):
    """One task submitted to an agent backend.

    ``id`` and ``instruction`` are the stable task identity and user-facing
    request.  The remaining fields carry optional execution context and keep
    the same shape used by the SDK-side task runners.
    """

    id: str
    """任务标识"""

    instruction: str
    """任务指令"""

    system_prompt: str | None = None
    """系统提示词"""

    tags: tuple[str, ...] = ()
    """任务标签"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """任务元数据"""


class ExecutionInfo(BaseModel):
    started_at: datetime
    """执行开始时间"""

    finished_at: datetime
    """执行结束时间"""

    duration_s: float
    """执行时长（秒）"""

    workspace: str | None = None
    """工作空间路径"""


class Trajectory(BaseModel):
    messages: list[dict[str, Any]]
    """轨迹消息记录"""

    input_skills: list[Skill]
    """输入技能列表"""

    used_skills: list[Skill]
    """使用技能列表"""

    started_at: float
    """执行开始时间（13位时间戳）"""

    finished_at: float
    """执行结束时间（13位时间戳）"""

    duration_s: float
    """执行总时长（秒）"""

    n_turn: int
    """执行轮次"""

    is_success: bool
    """任务是否成功"""

    error_info: str | None = None
    """如果有报错，存储错误信息"""
