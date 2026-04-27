"""Notifications 라우터 — S3 Phase 3 FG 3-3.

엔드포인트:
    GET  /api/v1/notifications                    — 본인 알림 목록 (?unread_only=&limit=)
    GET  /api/v1/notifications/unread-count       — 미읽음 카운트
    POST /api/v1/notifications/read               — body: {ids:[...]} — 읽음 처리

권한:
    - 모든 엔드포인트가 인증 필수.
    - 본인 알림만 조회/읽음 처리 (다른 사용자 알림은 service 가 SQL 안 user_id 필터로 자동 차단).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.models.annotation import Notification
from app.schemas.annotation import NotificationResponse, NotificationsMarkReadRequest
from app.services.notifications_service import notifications_service

router = APIRouter()


def _require_authenticated(actor: ActorContext) -> None:
    if not actor or not actor.is_authenticated or not actor.actor_id:
        raise ApiPermissionDeniedError("인증된 사용자만 알림을 조회할 수 있습니다.")


def _to_response(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=n.id,
        user_id=n.user_id,
        kind=n.kind,
        payload=n.payload or {},
        read_at=n.read_at,
        created_at=n.created_at,
    )


@router.get(
    "",
    summary="본인 알림 목록",
    response_model=SuccessResponse,
)
def list_notifications(
    request: Request,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        items = notifications_service.list_for_user(
            conn, actor, unread_only=unread_only, limit=limit,
        )
    return list_response(
        items=[_to_response(n).model_dump() for n in items],
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get(
    "/unread-count",
    summary="미읽음 알림 카운트",
    response_model=SuccessResponse,
)
def unread_count(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        count = notifications_service.count_unread(conn, actor)
    return success_response(
        data={"unread_count": int(count)},
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post(
    "/read",
    summary="알림 읽음 처리 (본인 알림만)",
    response_model=SuccessResponse,
)
def mark_read(
    body: NotificationsMarkReadRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated(actor)
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        updated = notifications_service.mark_read(conn, actor, body.ids)
    return success_response(
        data={"marked_read": int(updated)},
        request_id=request_id,
        trace_id=trace_id,
    )
