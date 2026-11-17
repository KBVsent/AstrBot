from __future__ import annotations

from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from astrbot.dashboard.responses import ApiError, ok
from astrbot.dashboard.schemas import (
    SnsMessageSendRequest,
    SnsSessionPatchRequest,
    SnsSessionRequest,
)
from astrbot.dashboard.services.sns_service import SnsService, SnsServiceError

from .auth import require_dashboard_user

# Dashboard login only: chat contents are not exposed to API keys.
router = APIRouter(prefix="/sns", tags=["SNS"], include_in_schema=False)


def get_service(request: Request) -> SnsService:
    return request.app.state.services.sns


def _raise(exc: SnsServiceError) -> NoReturn:
    raise ApiError(str(exc), status_code=exc.status_code) from exc


@router.get("/sessions")
async def list_sessions(
    message_type: Literal["GroupMessage", "FriendMessage", "OtherMessage"]
    | None = Query(default=None, alias="type"),
    platform_id: str | None = Query(default=None, max_length=255),
    q: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=2048),
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        return ok(
            await service.list_sessions(
                message_type=message_type,
                platform_id=platform_id,
                query=q.strip() if q else None,
                limit=limit,
                cursor=cursor,
            )
        )
    except SnsServiceError as exc:
        _raise(exc)


@router.patch("/session")
async def patch_session(
    payload: SnsSessionPatchRequest,
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        return ok(
            await service.patch_session(
                payload.umo, pinned=payload.pinned, keep_forever=payload.keep_forever
            )
        )
    except SnsServiceError as exc:
        _raise(exc)


@router.post("/session/read")
async def mark_session_read(
    payload: SnsSessionRequest,
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        return ok(await service.mark_read(payload.umo))
    except SnsServiceError as exc:
        _raise(exc)


@router.delete("/session")
async def delete_session(
    umo: str = Query(min_length=1, max_length=512),
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        await service.delete_session(umo)
    except SnsServiceError as exc:
        _raise(exc)
    return ok({"umo": umo})


@router.get("/messages")
async def list_messages(
    umo: str = Query(min_length=1, max_length=512),
    before_id: int | None = Query(default=None, ge=1),
    after_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        return ok(
            await service.list_messages(
                umo, before_id=before_id, after_id=after_id, limit=limit
            )
        )
    except SnsServiceError as exc:
        _raise(exc)


@router.post("/messages")
async def send_message(
    payload: SnsMessageSendRequest,
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        return ok(await service.send_text(payload.umo, payload.text, payload.client_id))
    except SnsServiceError as exc:
        _raise(exc)


@router.get("/image")
async def get_image(
    message_id: int = Query(ge=1),
    index: int = Query(ge=0),
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    try:
        image = await service.open_image(message_id, index)
    except SnsServiceError as exc:
        _raise(exc)
    # The sandbox keeps an opened SVG from running script on the dashboard origin.
    headers = {
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    }
    if image.path is not None:
        return FileResponse(image.path, media_type=image.media_type, headers=headers)
    assert image.chunks is not None and image.close is not None
    return StreamingResponse(
        image.chunks,
        media_type=image.media_type,
        headers=headers,
        background=BackgroundTask(image.close),
    )


@router.get("/events")
async def stream_events(
    _username: str = Depends(require_dashboard_user),
    service: SnsService = Depends(get_service),
):
    return StreamingResponse(
        service.stream_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
        },
    )
