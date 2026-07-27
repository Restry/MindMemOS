"""FastAPI dependencies for runtime access and standalone authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

from fastapi import Depends, Header, Request
from opentelemetry import trace

from ..config import bind_config_overrides
from ..errors import AuthenticationError, BadRequestError, PermissionDeniedError
from ..service.ports.memory import MemoryService
from ..service.schema import RequestContext
from .auth import ApiKeyProvider


async def get_memory_service(request: Request) -> MemoryService:
    """Return the service owned by the runtime started in FastAPI lifespan."""

    runtime = getattr(request.app.state, "mindmemos_runtime", None)
    if runtime is None:
        raise RuntimeError("MindMemOS runtime is not available")
    return runtime.memory


async def get_request_context(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AsyncIterator[RequestContext]:
    api_key = _extract_bearer_token(authorization)
    provider: ApiKeyProvider = request.app.state.api_key_provider
    resolved = provider.resolve(api_key)
    if resolved.memory_algorithm != "vanilla":
        raise BadRequestError(
            f"MindMemOS Lite only supports the vanilla memory algorithm; got {resolved.memory_algorithm!r}",
            code="unsupported_memory_algorithm",
        )

    context = RequestContext(
        request_id=str(uuid4()),
        account_id=resolved.account_id,
        project_id=resolved.project_id,
        api_key_uuid=resolved.key_id,
        memory_algorithm=resolved.memory_algorithm,
        scopes=resolved.scopes,
    )
    _annotate_trace(context)
    if resolved.project_config:
        with bind_config_overrides(project_config=resolved.project_config):
            yield context
        return
    yield context


def require_scopes(*required_scopes: str):
    async def dependency(context: RequestContext = Depends(get_request_context)) -> RequestContext:
        _ensure_scopes(context, required_scopes)
        return context

    return dependency


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthenticationError("missing Authorization header", code="auth.missing_authorization")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError(
            "expected 'Authorization: Bearer <api_key>'",
            code="auth.invalid_authorization_scheme",
        )
    return token.strip()


def _ensure_scopes(context: RequestContext, required_scopes: Sequence[str]) -> None:
    scopes = set(context.scopes)
    if "memory:*" in scopes:
        return
    missing = [scope for scope in required_scopes if scope not in scopes]
    if missing:
        raise PermissionDeniedError("insufficient scope", code="auth.insufficient_scope")


def _annotate_trace(context: RequestContext) -> None:
    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return
    span.set_attribute("request_id", context.request_id)
    span.set_attribute("account_id", context.account_id)
    span.set_attribute("project_id", context.project_id)
    span.set_attribute("api_key_uuid", context.api_key_uuid)


__all__ = ["get_memory_service", "get_request_context", "require_scopes"]
