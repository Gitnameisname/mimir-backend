"""Saved Views 라우터 — S3 Phase 2 FG 2-5.

엔드포인트 (5종):
    GET    /api/v1/saved-views              owner 본인 목록
    POST   /api/v1/saved-views              정의 저장 (owner = actor)
    GET    /api/v1/saved-views/{id}         정의 조회 (인증된 모든 사용자, owner_id 마스킹)
    PATCH  /api/v1/saved-views/{id}         owner 본인만
    DELETE /api/v1/saved-views/{id}         owner 본인만

ACL 정책:
    - GET 단건은 공유 URL 모델 — 인증된 모든 사용자가 정의를 볼 수 있다. 단 응답 모델
      `SavedViewResponse` 가 `owner_id` 자체를 미포함하므로 owner 식별 불가.
    - 결과의 ACL 은 본 라우터가 결정 안 함 — 클라이언트가 view 정의를 풀어 `/documents`
      를 호출하면 documents 가 viewer 의 ScopeProfile 로 재필터.
    - PATCH/DELETE 는 service 의 WHERE 절에서 owner_id 강제 — 다른 사용자 view_id 알아도 수정 불가.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.errors.exceptions import ApiAuthenticationError
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.models.saved_view import SavedView
from app.schemas.saved_views import (
    SavedViewCreateRequest,
    SavedViewResponse,
    SavedViewUpdateRequest,
)
from app.services.saved_views_service import saved_views_service

router = APIRouter()


def _to_response(view: SavedView) -> SavedViewResponse:
    """SavedView → 응답 직렬화. owner_id 자동 마스킹 (응답 모델 미포함)."""
    return SavedViewResponse(
        id=view.id,
        name=view.name,
        filter=view.filter,  # type: ignore[arg-type]
        sort=view.sort,  # type: ignore[arg-type]
        layout=view.layout,
        include_tag_nodes=view.include_tag_nodes,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _require_authenticated_actor_id(actor: ActorContext) -> str:
    if not actor.resolved_id:
        raise ApiAuthenticationError("로그인이 필요합니다")
    return actor.resolved_id


# ---------------------------------------------------------------------------
# GET /saved-views — owner 본인 목록
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="저장된 뷰 목록 (owner 본인)",
    response_model=SuccessResponse,
)
def list_saved_views(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated_actor_id(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        items, total = saved_views_service.list_for_owner(
            conn, owner_id=actor_id, page=page, page_size=page_size,
        )

    return list_response(
        data=[_to_response(v).model_dump(mode="json") for v in items],
        request_id=request_id,
        trace_id=trace_id,
        page=page,
        page_size=page_size,
        total=total,
    )


# ---------------------------------------------------------------------------
# POST /saved-views — 신규 저장
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="저장된 뷰 신규 생성",
    response_model=SuccessResponse,
    status_code=201,
)
def create_saved_view(
    body: SavedViewCreateRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated_actor_id(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        view = saved_views_service.create(conn, owner_id=actor_id, request=body)

    return success_response(
        data=_to_response(view).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /saved-views/{id} — 단건 (공유 URL 진입점, owner_id 마스킹)
# ---------------------------------------------------------------------------

@router.get(
    "/{view_id}",
    summary="저장된 뷰 단건 (공유 URL 진입점)",
    description=(
        "인증된 모든 사용자가 정의를 읽을 수 있다. 응답에 `owner_id` 는 포함되지 않음."
    ),
    response_model=SuccessResponse,
)
def get_saved_view(
    view_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated_actor_id(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        view = saved_views_service.get_for_share(conn, view_id)

    return success_response(
        data=_to_response(view).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# PATCH /saved-views/{id} — owner 본인만
# ---------------------------------------------------------------------------

@router.patch(
    "/{view_id}",
    summary="저장된 뷰 수정 (owner 본인만)",
    response_model=SuccessResponse,
)
def update_saved_view(
    view_id: str,
    body: SavedViewUpdateRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated_actor_id(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        view = saved_views_service.update(
            conn, view_id=view_id, owner_id=actor_id, request=body,
        )

    return success_response(
        data=_to_response(view).model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# DELETE /saved-views/{id} — owner 본인만
# ---------------------------------------------------------------------------

@router.delete(
    "/{view_id}",
    summary="저장된 뷰 삭제 (owner 본인만)",
    response_model=SuccessResponse,
)
def delete_saved_view(
    view_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    actor_id = _require_authenticated_actor_id(actor)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        saved_views_service.delete(conn, view_id=view_id, owner_id=actor_id)

    return success_response(
        data={"deleted": True, "id": view_id},
        request_id=request_id,
        trace_id=trace_id,
    )
