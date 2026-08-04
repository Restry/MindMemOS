from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..typing import Skill, SkillBinding, Task, Trajectory


@dataclass(slots=True)
class AgentExecutionRequest:
    task: Task
    """任务信息"""

    skills: list[Skill] = field(default_factory=list)
    """注入skill列表"""

    workspace: str = ""
    """工作目录"""

    metadata: dict[str, Any] = field(default_factory=dict)
    """其他信息"""


@dataclass(slots=True)
class AgentExecutionResult:
    execution_id: str
    task: Task
    skills: list[Skill]
    skill_bindings: list[SkillBinding]
    started_at: float
    ended_at: float
    duration_s: float
    validation: dict[str, Any] | None = None
    trajectory: Trajectory | None = None
    """完整执行轨迹"""
