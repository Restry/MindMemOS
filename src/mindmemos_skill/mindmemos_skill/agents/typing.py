from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..typing import Skill, SkillBinding, Task, Trajectory


@dataclass
class AgentExecutionRequest:
    task: Task
    skills: list[Skill]
    workspace: str
    metadata: dict[str, Any]
    max_turns: int | None = None
    """最大轮数限制"""


@dataclass
class AgentExecutionResult:
    execution_id: str
    task: Task
    skills: list[Skill]
    skill_bindings: list[SkillBinding]
    started_at: float
    ended_at: float
    duration_s: float
    validation: Any | None = None
    trajectory: Trajectory | None = None
    """完整执行轨迹"""
