"""FastAPI application factory backed by the shared Lite runtime."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..runtime import MindMemOS
from .auth import ApiKeyProvider, FileApiKeyProvider
from .routes import router as memory_router

RuntimeFactory = Callable[[], Any]


def create_app(
    *,
    runtime_factory: RuntimeFactory | None = None,
    api_key_provider: ApiKeyProvider | None = None,
) -> FastAPI:
    """Create an HTTP adapter that owns one regular ``MindMemOS`` runtime."""

    resolved_runtime_factory = runtime_factory or (lambda: MindMemOS.from_env(start_workers=True))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = resolved_runtime_factory()
        await runtime.start()
        try:
            app.state.mindmemos_runtime = runtime
            app.state.api_key_provider = api_key_provider or FileApiKeyProvider.from_env()
            yield
        finally:
            app.state.mindmemos_runtime = None
            await runtime.close()

    app = FastAPI(title="MindMemOS Lite API", version="0.1.0", lifespan=lifespan)
    _register_exception_handlers(app)

    @app.get("/healthz", tags=["meta"])
    async def healthz(request: Request) -> dict[str, str]:
        runtime = getattr(request.app.state, "mindmemos_runtime", None)
        state = getattr(runtime, "state", None)
        state_value = getattr(state, "value", state)
        return {"status": "ok", "runtime": str(state_value or "running")}

    app.include_router(memory_router)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "request_id": None, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_request",
                "message": _format_validation_errors(exc.errors()),
                "request_id": None,
                "data": None,
            },
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "bad_request", "message": str(exc), "request_id": None, "data": None},
        )

    @app.exception_handler(NotImplementedError)
    async def handle_not_implemented(_request: Request, exc: NotImplementedError) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"code": "not_implemented", "message": str(exc), "request_id": None, "data": None},
        )


def _format_validation_errors(errors: list[dict[str, Any]]) -> str:
    parts = []
    for error in errors:
        field = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{field}: {message}" if field else message)
    return "; ".join(parts) or "request validation failed"


app = create_app()

__all__ = ["app", "create_app"]
