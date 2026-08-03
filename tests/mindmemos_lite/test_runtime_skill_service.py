"""Composition-root tests for the optional Lite Skill service."""

from __future__ import annotations

import asyncio

import pytest
from mindmemos_lite.runtime import MindMemOS, MindMemOSState


@pytest.mark.asyncio
async def test_runtime_exposes_injected_skill_service() -> None:
    service = object()
    runtime = MindMemOS(skill_service=service)
    runtime._state = MindMemOSState.RUNNING
    runtime._loop = asyncio.get_running_loop()

    assert runtime.skill is service


@pytest.mark.asyncio
async def test_runtime_does_not_instantiate_skill_protocol() -> None:
    runtime = MindMemOS()
    runtime._state = MindMemOSState.RUNNING
    runtime._loop = asyncio.get_running_loop()

    with pytest.raises(RuntimeError, match="inject a concrete SkillService"):
        runtime.skill
