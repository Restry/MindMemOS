"""Thin HTTP routes over the runtime-owned memory service."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..service.ports.memory import MemoryService
from ..service.schema import RequestContext
from .deps import get_memory_service, require_scopes
from .mappers import (
    to_add_command,
    to_add_response,
    to_delete_command,
    to_dreaming_command,
    to_feedback_command,
    to_get_command,
    to_memory_list_response,
    to_search_command,
    to_status_response,
    to_update_command,
    with_actor_identity,
)
from .schemas import (
    AddData,
    AddRequest,
    ApiResponse,
    DeleteRequest,
    DreamingRequest,
    FeedbackRequest,
    GetRequest,
    MemoryListData,
    SearchRequest,
    UpdateRequest,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])
SCOPE_MEMORY_READ = "memory:read"
SCOPE_MEMORY_WRITE = "memory:write"


@router.post("/add", response_model=ApiResponse[AddData])
async def add_memory(
    payload: AddRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_WRITE)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[AddData]:
    context = with_actor_identity(context, payload)
    return to_add_response(await service.add(context, to_add_command(payload)), context.request_id)


@router.post("/search", response_model=ApiResponse[MemoryListData])
async def search_memory(
    payload: SearchRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_READ)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[MemoryListData]:
    context = with_actor_identity(context, payload)
    result = await service.search(context, to_search_command(payload))
    return to_memory_list_response(result, context.request_id)


@router.post("/get", response_model=ApiResponse[MemoryListData])
async def get_memory(
    payload: GetRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_READ)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[MemoryListData]:
    return to_memory_list_response(await service.get(context, to_get_command(payload)), context.request_id)


@router.post("/delete", response_model=ApiResponse[None])
async def delete_memory(
    payload: DeleteRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_WRITE)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[None]:
    return to_status_response(await service.delete(context, to_delete_command(payload)), context.request_id)


@router.post("/update", response_model=ApiResponse[None])
async def update_memory(
    payload: UpdateRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_WRITE)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[None]:
    return to_status_response(await service.update(context, to_update_command(payload)), context.request_id)


@router.post("/feedback", response_model=ApiResponse[None])
async def feedback_memory(
    payload: FeedbackRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_WRITE)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[None]:
    context = with_actor_identity(context, payload)
    result = await service.feedback(context, to_feedback_command(payload))
    return ApiResponse[None](
        code=result.status,
        message=result.message or "",
        request_id=context.request_id,
        data=None,
    )


@router.post("/dreaming", response_model=ApiResponse[None])
async def dreaming_memory(
    payload: DreamingRequest,
    context: RequestContext = Depends(require_scopes(SCOPE_MEMORY_WRITE)),
    service: MemoryService = Depends(get_memory_service),
) -> ApiResponse[None]:
    context = with_actor_identity(context, payload)
    result = await service.dream(context, to_dreaming_command(payload))
    return to_status_response(result, context.request_id)


__all__ = ["router"]
