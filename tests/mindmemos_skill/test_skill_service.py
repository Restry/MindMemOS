from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from mindmemos_skill.service import (
    MindMemosSkill,
    SkillAlgorithms,
    SkillAnalysisRequest,
    SkillAnalysisResult,
    SkillCapabilityUnavailableError,
    SkillFinding,
    SkillOptimizationRequest,
    SkillOptimizationResult,
    SkillServiceClosedError,
    SkillServiceComponents,
)
from mindmemos_skill.typing import Skill


def make_skill(content: str = "Use the API carefully.") -> Skill:
    return Skill(name="api-helper", description="Helps call an API", content=content)


class FakeAnalyzer:
    def __init__(self) -> None:
        self.requests: list[SkillAnalysisRequest] = []

    async def analyze(self, request: SkillAnalysisRequest) -> SkillAnalysisResult:
        self.requests.append(request)
        return SkillAnalysisResult(
            summary="One ambiguous instruction",
            findings=[SkillFinding(category="clarity", message="Specify the API response contract")],
        )


class FakeOptimizer:
    def __init__(self) -> None:
        self.requests: list[SkillOptimizationRequest] = []

    async def optimize(self, request: SkillOptimizationRequest) -> SkillOptimizationResult:
        self.requests.append(request)
        optimized = request.skill.model_copy(
            update={"content": request.skill.content + "\nValidate the response schema."}
        )
        return SkillOptimizationResult(skill=optimized, changed=True, analysis=request.analysis)


class FakeFactory:
    def __init__(self, components: SkillServiceComponents) -> None:
        self.components = components
        self.config: Any = None
        self.environ: Mapping[str, str] | None = None

    def from_config(self, config: Any) -> SkillServiceComponents:
        self.config = config
        return self.components

    def from_env(self, environ: Mapping[str, str]) -> SkillServiceComponents:
        self.environ = environ
        return self.components


@pytest.mark.asyncio
async def test_facade_delegates_analyze_and_optimize_and_starts_once() -> None:
    analyzer = FakeAnalyzer()
    optimizer = FakeOptimizer()
    lifecycle: list[str] = []

    async def start() -> None:
        lifecycle.append("start")

    service = MindMemosSkill(
        analyzer=analyzer,
        optimizer=optimizer,
        start=start,
    )
    skill = make_skill()

    analysis = await service.analyze(SkillAnalysisRequest(skill=skill))
    result = await service.optimize(SkillOptimizationRequest(skill=skill, analysis=analysis))

    assert lifecycle == ["start"]
    assert analyzer.requests[0].skill == skill
    assert optimizer.requests[0].analysis == analysis
    assert result.changed is True
    assert "Validate the response schema." in result.skill.content
    assert service.capabilities == frozenset({"analyze", "optimize"})
    assert service.algorithms.capabilities == service.capabilities


@pytest.mark.asyncio
async def test_algorithm_api_delegates_without_owning_lifecycle() -> None:
    analyzer = FakeAnalyzer()
    algorithms = SkillAlgorithms(analyzer=analyzer)
    request = SkillAnalysisRequest(skill=make_skill())

    result = await algorithms.analyze(request)

    assert result.summary == "One ambiguous instruction"
    assert analyzer.requests == [request]
    assert algorithms.capabilities == frozenset({"analyze"})
    assert not hasattr(algorithms, "start")
    assert not hasattr(algorithms, "close")
    assert not hasattr(algorithms, "from_config")


def test_runtime_can_be_composed_from_a_pure_algorithm_api() -> None:
    algorithms = SkillAlgorithms(analyzer=FakeAnalyzer())

    service = MindMemosSkill(algorithms=algorithms)

    assert service.algorithms is algorithms
    assert service.capabilities == frozenset({"analyze"})


def test_runtime_rejects_mixing_algorithm_api_and_individual_capabilities() -> None:
    with pytest.raises(ValueError, match="algorithms or individual capabilities"):
        MindMemosSkill(algorithms=SkillAlgorithms(analyzer=FakeAnalyzer()), analyzer=FakeAnalyzer())


@pytest.mark.asyncio
async def test_concurrent_operations_initialize_runtime_once() -> None:
    analyzer = FakeAnalyzer()
    starts = 0

    async def start() -> None:
        nonlocal starts
        await asyncio.sleep(0)
        starts += 1

    service = MindMemosSkill(analyzer=analyzer, start=start)
    request = SkillAnalysisRequest(skill=make_skill())

    await asyncio.gather(service.analyze(request), service.analyze(request))

    assert starts == 1
    assert len(analyzer.requests) == 2


@pytest.mark.asyncio
async def test_missing_capability_raises_clear_error() -> None:
    service = MindMemosSkill(analyzer=FakeAnalyzer())

    with pytest.raises(SkillCapabilityUnavailableError, match="optimization"):
        await service.optimize(SkillOptimizationRequest(skill=make_skill()))


@pytest.mark.asyncio
async def test_factory_constructors_keep_config_resolution_outside_facade() -> None:
    factory = FakeFactory(SkillServiceComponents(analyzer=FakeAnalyzer()))

    from_config = MindMemosSkill.from_config({"profile": "customer-local"}, factory=factory)
    from_env = MindMemosSkill.from_env(
        factory=factory,
        environ={"MINDMEMOS_SKILL_PROFILE": "customer-local", "MODEL_API_KEY": "secret"},
    )

    assert factory.config == {"profile": "customer-local"}
    assert factory.environ == {
        "MINDMEMOS_SKILL_PROFILE": "customer-local",
        "MODEL_API_KEY": "secret",
    }
    assert from_config.capabilities == frozenset({"analyze"})
    assert from_env.capabilities == frozenset({"analyze"})


@pytest.mark.asyncio
async def test_async_context_closes_resources_and_rejects_later_calls() -> None:
    lifecycle: list[str] = []

    async def start() -> None:
        lifecycle.append("start")

    async def close() -> None:
        lifecycle.append("close")

    service = MindMemosSkill(
        analyzer=FakeAnalyzer(),
        start=start,
        close=close,
    )

    async with service:
        await service.analyze(SkillAnalysisRequest(skill=make_skill()))

    await service.close()
    assert lifecycle == ["start", "close"]

    with pytest.raises(SkillServiceClosedError, match="closed"):
        await service.analyze(SkillAnalysisRequest(skill=make_skill()))


def test_service_requires_at_least_one_capability() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MindMemosSkill()
