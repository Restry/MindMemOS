from fastapi import FastAPI
from fastapi.testclient import TestClient
from mindmemos.api.app import register_exception_handlers
from mindmemos.api.schemas import AddRequest
from mindmemos.errors import BadRequestError, MemoryExtractionError


def test_request_validation_errors_return_one_message() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/add")
    async def add(payload: AddRequest):
        return {"code": "ok", "data": None}

    response = TestClient(app).post("/add", json={"user_id": "u1", "messages": []})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["data"] is None
    assert "body.messages" in body["message"]


def test_api_error_returns_one_message() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/bad")
    async def bad():
        raise BadRequestError("top_k must be <= 100; value=101", code="search.top_k_too_large")

    response = TestClient(app).get("/bad")

    assert response.status_code == 400
    assert response.json() == {
        "code": "search.top_k_too_large",
        "message": "top_k must be <= 100; value=101",
        "data": None,
    }


def test_add_request_accepts_structured_file_source_bound_to_message() -> None:
    request = AddRequest.model_validate(
        {
            "user_id": "u1",
            "messages": [{"role": "user", "content": "退款期限为七天。"}],
            "sources": [
                {
                    "source_type": "file",
                    "file_name": "policy.pdf",
                    "content_hash": "sha256:test",
                    "chunk_id": "chunk-2",
                    "metadata": {"message_index": 0, "chunk_index": 2},
                }
            ],
        }
    )
    assert request.sources[0].source_type == "file"
    assert request.sources[0].metadata["message_index"] == 0


def test_add_request_rejects_source_with_invalid_message_index() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/add-source")
    async def add_source(payload: AddRequest):
        return {"code": "ok"}

    response = TestClient(app).post(
        "/add-source",
        json={
            "user_id": "u1",
            "messages": [{"role": "user", "content": "content"}],
            "sources": [{"source_type": "file", "metadata": {"message_index": 3}}],
        },
    )
    assert response.status_code == 422
    assert "metadata.message_index" in response.json()["message"]


def test_memory_extraction_error_is_structured_retryable_503() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/extract")
    async def extract():
        raise MemoryExtractionError(
            "memory extraction failed",
            chunk_index=4,
            boundary="open_tail",
            attempts=3,
            retryable=True,
        )

    response = TestClient(app).get("/extract")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "memory_extraction_failed"
    assert body["data"]["failure"] == {
        "error_code": "memory_extraction_failed",
        "error_stage": "extract",
        "chunk_index": 4,
        "boundary": "open_tail",
        "attempts": 3,
        "retryable": True,
    }
