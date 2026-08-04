"""Translate agent execution state into the algorithm-facing trajectory."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..typing import (
    AgentProfile,
    AgentType,
    Environment,
    ExecutionInfo,
    Rollout,
    SkillBinding,
    Trajectory,
    TrajectoryStatus,
)
from .config import AgentConfig
from .typing import AgentExecutionRequest


def build_trajectory(
    *,
    request: AgentExecutionRequest,
    config: AgentConfig,
    execution_id: str,
    messages: list[dict[str, Any]],
    skill_bindings: list[SkillBinding],
    started_at: float,
    ended_at: float,
    n_turn: int,
    is_success: bool,
    error_info: str | None,
    agent_type: AgentType,
) -> Trajectory:
    """Build one normalized trajectory from a concrete agent execution."""
    rollout_id = request.metadata.get("rollout_id")
    if not isinstance(rollout_id, str) or not rollout_id:
        rollout_id = execution_id
    attempt_no = request.metadata.get("attempt_no", 0)
    if not isinstance(attempt_no, int) or isinstance(attempt_no, bool) or attempt_no < 0:
        attempt_no = 0

    return Trajectory(
        id=execution_id,
        task=request.task,
        rollout=Rollout(id=rollout_id, attempt_no=attempt_no),
        environment=Environment(running_dir=request.workspace or None),
        agent=AgentProfile(agent_type=agent_type, config=config.snapshot()),
        injected_skills=request.skills,
        events=messages,
        skill_bindings=skill_bindings,
        execution=ExecutionInfo(
            status=TrajectoryStatus.SUCCEEDED if is_success else TrajectoryStatus.FAILED,
            started_at=datetime.fromtimestamp(started_at, tz=UTC),
            finished_at=datetime.fromtimestamp(ended_at, tz=UTC),
            n_turn=n_turn,
            error_info=error_info,
        ),
    )


__all__ = ["build_trajectory"]
