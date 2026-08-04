"""Aggregated trajectory contracts consumed by Skill algorithms."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .agent import AgentProfile
from .env import Environment, Reward
from .skill import Skill, SkillBinding
from .task import Task


class RolloutType(StrEnum):
    """Business purpose of one planned rollout."""

    TRAIN = "train"
    EVALUATE = "evaluate"
    TEST = "test"
    INFERENCE = "inference"


class TrajectoryStatus(StrEnum):
    """Lifecycle state of one physical rollout attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Rollout(BaseModel):
    """Stable rollout identity plus the current retry attempt."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1)
    """一次计划 rollout 的稳定标识；其所有重试共享该值。"""

    attempt_no: int = Field(default=0, ge=0)
    """当前物理尝试的序号，首次执行为 0。"""

    rollout_type: RolloutType = RolloutType.INFERENCE
    """本次 rollout 的训练、评估、测试或推理用途。"""


class ExecutionInfo(BaseModel):
    """Runtime outcome of one physical trajectory attempt."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    status: TrajectoryStatus = TrajectoryStatus.RUNNING
    """当前 attempt 的执行状态。"""

    started_at: datetime
    """执行开始时间。"""

    finished_at: datetime | None = None
    """执行结束时间；尚未结束时为空。"""

    n_turn: int = Field(default=0, ge=0)
    """Agent 交互轮数。"""

    error_info: str | None = None
    """执行失败、工具异常或环境错误信息。"""

    @property
    def duration_s(self) -> float | None:
        """Duration derived from the two timestamps when the attempt finished."""

        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @model_validator(mode="after")
    def validate_timestamps(self) -> ExecutionInfo:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self


class Trajectory(BaseModel):
    """Algorithm-facing aggregate for one physical Agent execution attempt.

    Persistence stores this aggregate as one flattened ``TrajectoryRecord``:
    task, rollout, environment, Agent, reward and execution fields become
    columns, while events and Skill snapshots remain JSON columns.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1)
    """某一次实际执行 attempt 的唯一标识。"""

    task: Task
    """逻辑任务及其输入上下文。"""

    rollout: Rollout
    """计划 rollout 与重试信息。"""

    environment: Environment = Field(default_factory=Environment)
    """任务运行目录和环境元数据。"""

    agent: AgentProfile = Field(default_factory=AgentProfile)
    """执行轨迹的 Agent 类型及可复现配置。"""

    injected_skills: list[Skill] = Field(default_factory=list)
    """执行开始前提供给 Agent 的完整 Skill。"""

    events: list[dict[str, JsonValue]] = Field(default_factory=list)
    """按发生顺序记录的消息、工具调用和内部事件。"""

    skill_bindings: list[SkillBinding] = Field(default_factory=list)
    """本次执行实际使用到的 Skill 版本引用。"""

    reward: Reward | None = None
    """尚未评估时为空的结构化评分结果。"""

    execution: ExecutionInfo
    """该物理 attempt 的状态、时间、轮数和错误。"""

    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """数据集、采集器和算法附加信息。"""


__all__ = [
    "ExecutionInfo",
    "Rollout",
    "RolloutType",
    "Trajectory",
    "TrajectoryStatus",
]
