from mindmemos.api.mappers import to_search_api_response
from mindmemos.typing.service import (
    MemorySearchItem,
    MemorySearchResultItem,
    SearchPipelineResult,
    SearchRelevance,
)


def test_search_response_omits_relevance_for_legacy_results() -> None:
    response = to_search_api_response(
        SearchPipelineResult(
            status="ok",
            memories=[MemorySearchItem(id="memory-1", memory="content", last_update_at="")],
        ),
        "request-1",
    )

    payload = response.model_dump(mode="json")
    assert payload["data"]["memories"] == [
        {
            "id": "memory-1",
            "memory": "content",
            "memory_type": "fact",
            "last_update_at": "",
            "event_time": None,
            "source_timestamp": None,
            "lineage": None,
            "metadata": {},
            "status": None,
            "entity_id": None,
            "entity_type": None,
            "property_name": None,
        }
    ]


def test_search_response_retains_opt_in_query_local_relevance() -> None:
    response = to_search_api_response(
        SearchPipelineResult(
            status="ok",
            memories=[
                MemorySearchResultItem(
                    id="memory-1",
                    memory="content",
                    last_update_at="",
                    relevance=SearchRelevance(
                        score=1.0,
                        source="retrieval",
                        rank=0,
                        retrieval_score=3.5,
                        retrieval_score_type="bm25",
                    ),
                )
            ],
        ),
        "request-1",
    )

    relevance = response.model_dump(mode="json")["data"]["memories"][0]["relevance"]
    assert relevance["scope"] == "query_local"
    assert relevance["retrieval_score"] == 3.5
    assert relevance["retrieval_score_type"] == "bm25"
