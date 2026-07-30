from types import SimpleNamespace

import pytest
from mindmemos.api import mappers as api_mappers
from mindmemos.api.schemas import SearchRequest
from mindmemos.config import SearchConfig
from mindmemos.errors import BadRequestError


def test_search_patches_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    search_config = SearchConfig()
    monkeypatch.setattr(
        api_mappers,
        "get_config",
        lambda: SimpleNamespace(algo_config=SimpleNamespace(search=search_config)),
    )

    inp = api_mappers.to_search_pipeline_input(
        SearchRequest(user_id="u1", query="Qdrant"),
        search_pipline="vanilla",
    )

    assert search_config.include_patches is True
    assert inp.include_patches is True


def test_search_token_budget_uses_configured_public_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    search_config = SearchConfig()
    search_config.retention.min_token_budget = 100
    search_config.retention.max_token_budget = 1000
    monkeypatch.setattr(
        api_mappers,
        "get_config",
        lambda: SimpleNamespace(algo_config=SimpleNamespace(search=search_config)),
    )

    with pytest.raises(BadRequestError) as exc_info:
        api_mappers.to_search_pipeline_input(
            SearchRequest(user_id="u1", query="Qdrant", token_budget=1001),
            search_pipline="vanilla",
        )

    assert exc_info.value.code == "search.token_budget_out_of_range"
