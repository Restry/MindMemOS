"""Algorithm identity and step-report aggregates."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class AlgorithmIdentity(BaseModel):
    """Stable description of the algorithm implementation producing a report."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    """算法名称，例如 trace_summary、skillopt 或 merge_resolver。"""

    version: str | None = None
    """算法实现、配置或 Prompt 协议版本。"""


class AlgorithmStep(BaseModel):
    """One component step emitted during an algorithm execution."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    component_name: str = Field(min_length=1)
    """上报步骤的算法组件。"""

    name: str = Field(min_length=1)
    """组件内注册的步骤名称。"""

    status: str | None = None
    """started、succeeded、rejected、failed 等组件自定义状态。"""

    payload: dict[str, JsonValue] = Field(default_factory=dict)
    """该步骤的输入、输出、指标、决策、错误和 artifact 引用。"""

    created_at: datetime
    """步骤报告产生时间。"""


class AlgorithmLog(BaseModel):
    """Algorithm-facing report aggregate stored as one flat log row.

    ``AlgorithmLogRecord`` flattens ``algorithm`` and ``step`` into columns.
    This object deliberately represents one step only because the persistence
    contract currently has no algorithm-run identifier for grouping rows.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1)
    algorithm: AlgorithmIdentity
    step: AlgorithmStep


__all__ = ["AlgorithmIdentity", "AlgorithmLog", "AlgorithmStep"]
