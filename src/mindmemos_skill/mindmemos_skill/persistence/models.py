"""Minimal persistence contracts for local Skill algorithms.

``SkillRecord``, ``TrajectoryRecord``, and ``AlgorithmLogRecord`` each describe
one flat database row.  JSON columns use plain JSON-compatible dictionaries or
lists rather than nested Pydantic models.

This module contains data contracts only.  It does not open SQLite, select a
vector backend, or depend on ``mindmemos_sdk``.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from mindmemos.typing import SkillBinding
from mindmemos_skill.typing import Skill


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class PersistenceModel(BaseModel):
    """Strict base model shared by the three flat database-row records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SkillVersionStatus(StrEnum):
    DRAFT = "draft"
    """Skill仍在草稿阶段，未正式发布"""

    REJECTED = "rejected"
    """Skill被算法门控拒绝"""

    PUBLISHED = "published"
    """Skill发布，可用状态"""

    ARCHIVED = "archived"
    """Skill已失效，归档状态"""


class SkillVersionOrigin(StrEnum):
    LOCAL = "local"
    """由本地产生"""

    CLOUD = "cloud"
    """由云端同步"""

    EVOLUTION = "evolution"
    """由Skill演进算法发布"""

    MANAGE = "manage"
    """由Skill管理算法发布"""


class TrajectoryStatus(StrEnum):
    RUNNING = "running"
    """轨迹执行中"""

    SUCCEEDED = "succeeded"
    """轨迹执行成功"""

    FAILED = "failed"
    """轨迹执行失败"""

    CANCELLED = "cancelled"
    """轨迹执行被人工取消"""


class RolloutType(StrEnum):
    """Business purpose of one rollout."""

    TRAIN = "train"
    EVALUATE = "evaluate"
    TEST = "test"
    INFERENCE = "inference"


class AgentType(StrEnum):
    """Business purpose of one agent type."""
    CLAUDE = "claude"
    CLAUDE_SDK = "clause_sdk"
    CODEX = "codex"
    OPENCLAW = "openclaw"
    OPENCODE = "opencode"
    GEMINI_CLI = "gemini_cli"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class SkillRecord(PersistenceModel):
    """One table row representing one immutable version of a registered Skill."""

    # ------------- 版本号 -------------
    skill_id: str = Field(min_length=1)
    """Skill 家族标识；同一个 Skill 的所有版本共享该值。"""

    version_id: str = Field(min_length=1)
    """当前不可变版本的唯一标识；一条数据库记录对应一个 version_id。"""

    cloud_skill_id: str | None = None
    """与云端 Skill 家族关联的可选标识。"""

    parent_version_ids: list[str] = Field(default_factory=list)
    """当前版本的直接父版本集合；根版本为空，多父表示合并。"""

    # ------------- Skill核心内容 -------------
    name: str = Field(min_length=1)
    """Skill 的展示名称。"""

    description: str | None = None
    """Skill 功能描述"""

    alias: str | None = None
    """便于 CLI 和人工检索的可选短名称。"""

    blob: str = Field(min_length=1)
    """核心 Skill 文件的 JSON 文本，结构为相对路径到文件内容的映射。"""

    resources: str = "{}"
    """辅助资源文件的 JSON 文本，与 blob 使用相同结构。"""

    content_hash: str = Field(min_length=1)
    """Skill 核心BUNDLE 规范化后的内容哈希。"""

    status: SkillVersionStatus = SkillVersionStatus.DRAFT
    """当前 Skill 版本的生命周期状态。"""

    version_label: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
        examples=["1.0.0", "1.2.3"],
    )
    """人工可读版本标签；格式固定为 x.x.x。"""

    commit_message: str | None = None
    """创建该版本时记录的变更说明。"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """算法运行、候选来源和其他扩展信息。"""

    # ------------- 溯源信息 -------------
    created_at: datetime = Field(default_factory=utcnow)
    """该版本记录首次写入数据库的时间。"""

    origin: SkillVersionOrigin = SkillVersionOrigin.LOCAL
    """版本来源；区分本机创建、云端同步、算法演进或合并。"""

    @field_validator("blob", "resources")
    @classmethod
    def validate_serialized_files(cls, value: str) -> str:
        _parse_serialized_files(value)
        return value

    @model_validator(mode="after")
    def validate_parent_ids(self) -> SkillRecord:
        if self.version_id in self.parent_version_ids:
            raise ValueError("a Skill version cannot be its own parent")
        if len(self.parent_version_ids) != len(set(self.parent_version_ids)):
            raise ValueError("parent_version_ids may not contain duplicates")
        return self


class TrajectoryRecord(PersistenceModel):
    """One flat table row containing one physical Agent rollout attempt."""

    # ------------- 轨迹溯源 -------------
    trajectory_id: str = Field(min_length=1)
    """某一次实际执行 attempt 的唯一标识。"""

    task_id: str = Field(min_length=1)
    """本次执行的任务标识。"""

    rollout_id: str = Field(min_length=1)
    """一次计划 rollout 的稳定标识；重试共享该值。"""

    attempt_no: int = Field(default=0, ge=0)
    """同一 rollout_id 下的尝试序号；首次为 0。"""

    rollout_type: RolloutType = RolloutType.INFERENCE
    """rollout 用途；区分训练、评估、测试和普通推理。"""

    # ------------- 任务信息 -------------
    task_instruction: str = Field(min_length=1)
    """提交给 Agent 的任务指令。"""

    task_system_prompt: str | None = None
    """本次任务使用的系统提示词快照。"""

    task_tags: list[str] = Field(default_factory=list)
    """任务标签 JSON 列。"""

    task_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """数据集、环境和调用方附加的任务信息 JSON 列。"""

    running_dir: str | None = None
    """运行工作目录"""

    env_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """执行该任务的env环境信息"""

    injected_skills: list[Skill] = Field(default_factory=list)
    """执行该任务注入的skill列表"""

    # ------------- Agent信息 -------------
    agent_type: AgentType = AgentType.UNKNOWN
    """执行任务的 Agent 实现类型。"""

    agent_profile: dict[str, Any] = Field(default_factory=dict)
    """模型、提供商、温度和推理参数等非密钥配置快照。"""

    # ------------- 轨迹详情 -------------
    status: TrajectoryStatus = TrajectoryStatus.RUNNING
    """该 rollout attempt 的执行状态。"""

    trajectory: list[SkillBinding] = Field(default_factory=list)
    """按发生顺序保存的消息、工具和内部事件 JSON 列。"""

    skill_bindings: list[dict[str, JsonValue]] = Field(default_factory=list)
    """本次执行实际使用的 Skill 绑定信息 JSON 列。"""

    # ------------- Reward信息 -------------
    reward_score: float | None = None
    """本次执行的奖励分数；尚未评分时为空。"""

    reward_detail: str | None = None
    """评分过程、通过情况或失败原因等说明。"""

    reward_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    """评估器、指标名称、阈值和其他评分信息 JSON 列。"""

    # ------------- 运行信息 -------------
    started_at: datetime = Field(default_factory=utcnow)
    """该 attempt 开始执行的时间。"""

    finished_at: datetime | None = None
    """该 attempt 结束的时间。"""

    n_turn: int = Field(default=0, ge=0)
    """该 attempt 消耗的 Agent 交互轮数。"""

    error_info: str | None = None
    """执行失败、工具异常或环境错误信息。"""

    # ------------- 其他非固定信息 -------------
    metadata: dict[str, Any] = Field(default_factory=dict)
    """数据集、采集器和 Agent 附加信息。"""

    @model_validator(mode="after")
    def validate_values(self) -> TrajectoryRecord:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.reward_score is not None and not math.isfinite(self.reward_score):
            raise ValueError("reward_score must be a finite number")
        return self


class AlgorithmLogRecord(PersistenceModel):
    """One generic step report emitted by an algorithm component.

    Algorithms decide which steps to report and place step-specific inputs,
    outputs, metrics, decisions, errors, and artifact references in ``payload``.
    The table keeps only the common fields needed to order and locate reports.
    """

    log_id: str = Field(min_length=1)
    """单条算法步骤报告的唯一标识。"""

    algorithm_name: str = Field(min_length=1)
    """算法名称，例如 trace_summary、skillopt 或 merge_resolver。"""

    algorithm_version: str | None = None
    """算法实现或 Prompt 协议版本。"""

    component_name: str = Field(min_length=1)
    """上报该步骤的算法组件名称。"""

    step_name: str = Field(min_length=1)
    """组件注册的步骤名称。"""

    status: str | None = None
    """组件自定义的步骤状态，例如 started、succeeded、rejected 或 failed。"""

    payload: dict[str, JsonValue] = Field(default_factory=dict)
    """步骤具体要报告的信息，以 JSON 存储。"""

    created_at: datetime = Field(default_factory=utcnow)
    """该步骤报告写入数据库的时间。"""


def _parse_serialized_files(value: str) -> dict[str, str]:
    """Parse one serialized multi-file field and validate its portable shape."""

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("serialized files must be valid JSON") from exc
    if not isinstance(parsed, dict) or any(
            not isinstance(path, str) or not path or not isinstance(content, str) for path, content in parsed.items()
    ):
        raise ValueError("serialized files must be a JSON object mapping non-empty paths to text content")
    return parsed


__all__ = [
    "AlgorithmLogRecord",
    "PersistenceModel",
    "RolloutType",
    "SkillRecord",
    "SkillVersionOrigin",
    "SkillVersionStatus",
    "TrajectoryRecord",
    "TrajectoryStatus",
    "utcnow",
]
