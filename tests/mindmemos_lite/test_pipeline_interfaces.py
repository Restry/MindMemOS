from dataclasses import dataclass

import pytest
from mindmemos_lite.pipeline import MemoryPersistencePipelineMixin, PipelineBase, create_pipeline, register


@dataclass(frozen=True)
class DummyConfig:
    marker: str


def test_registered_pipeline_uses_its_from_config_constructor() -> None:
    @register(type="add", name="test_from_config_add")
    class TestAddPipeline(PipelineBase):
        @classmethod
        def from_config(cls, config, **kwargs):
            return cls(marker=config.marker, dependency=kwargs["dependency"])

        def __init__(self, *, marker: str, dependency: str):
            self.marker = marker
            self.dependency = dependency

    pipeline = create_pipeline(
        type="add",
        name="test_from_config_add",
        config=DummyConfig(marker="project-config"),
        dependency="injected",
    )

    assert pipeline.marker == "project-config"
    assert pipeline.dependency == "injected"


def test_pipeline_base_default_constructor_keeps_simple_algorithms_compatible() -> None:
    @register(type="get", name="test_default_get")
    class TestGetPipeline(PipelineBase):
        def __init__(self, *, marker: str):
            self.marker = marker

    pipeline = create_pipeline(
        type="get",
        name="test_default_get",
        config=DummyConfig(marker="unused"),
        marker="constructor-value",
    )

    assert pipeline.marker == "constructor-value"


def test_memory_persistence_pipeline_mixin_only_exposes_persistence() -> None:
    persistence = object()

    pipeline = MemoryPersistencePipelineMixin(persistence=persistence)

    assert pipeline.persistence is persistence
    assert not hasattr(pipeline, "recorder")
    assert not hasattr(pipeline, "db_reader")
    assert not hasattr(pipeline, "db_writer")


@pytest.mark.parametrize("legacy_dependency", ["db_reader", "db_writer"])
def test_memory_persistence_pipeline_mixin_rejects_legacy_dependencies(legacy_dependency: str) -> None:
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{legacy_dependency}'"):
        MemoryPersistencePipelineMixin(persistence=object(), **{legacy_dependency: object()})


def test_memory_persistence_pipeline_mixin_rejects_service_orchestration_dependencies() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'recorder'"):
        MemoryPersistencePipelineMixin(persistence=object(), recorder=object())
