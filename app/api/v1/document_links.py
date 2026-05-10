"""Document Links 라우터 — S3 Phase 2 FG 2-3.

엔드포인트:
    GET /api/v1/documents/resolve?q=             — TipTap WikiLinkMark 자동완성 (viewer Scope)
    GET /api/v1/documents/{document_id}/backlinks — 이 문서를 참조하는 출발 문서 목록
    GET /api/v1/documents/{document_id}/links     — admin 전용 정방향 (디버깅)

ACL:
    /backlinks / /resolve — viewer Scope 자동 필터 (documents_repository.scope 헬퍼와 동일 정본).
    /links — ORG_ADMIN / SUPER_ADMIN role 만 (admin authorization).

라우트 등록 순서:
    `/resolve` 가 `/{document_id}` 보다 먼저 정의되어야 path 충돌이 없다 (FastAPI 는
    declaration 순서대로 매칭).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.repositories.document_links_repository import document_links_repository
from app.repositories.documents_repository import documents_repository
from app.schemas.document_links import BacklinkItem, OutgoingLinkItem, ResolveItem
from app.services.documents_service import _resolve_viewer_scope_profile_ids
from app.services.wikilink_resolver import normalize_title
from app.utils.http_errors import not_found_resource

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /resolve  — declaration 순서상 /{document_id} 보다 먼저
# ---------------------------------------------------------------------------

@router.get(
    "/resolve",
    summary="문서 제목 자동완성 (TipTap WikiLinkMark 입력)",
    description=(
        "``[[`` 입력 시 호출되는 자동완성 엔드포인트. "
        "viewer Scope 안의 문서만 후보로 반환한다 (다른 Scope 의 문서는 존재 자체가 누설되지 않음)."
    ),
    response_model=SuccessResponse,
    tags=["wikilinks"],
)
def resolve_document_title(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="prefix 매칭 대상 (NFC 정규화 적용)"),
    limit: int = Query(default=10, ge=1, le=20),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.list",  # 본질적으로 문서 목록의 부분 view
        resource=ResourceRef(resource_type="document"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    scope_ids = _resolve_viewer_scope_profile_ids(actor)

    # NFC 정규화 — DB 의 title 도 NFC 정규화 후 비교 (resolver 와 동일 정책)
    q_normalized = normalize_title(q)

    with get_db() as conn:
        rows = document_links_repository.resolve_title_prefix(
            conn,
            q=q_normalized,
            viewer_scope_profile_ids=scope_ids,
            limit=limit,
        )

    items = [
        ResolveItem(
            id=row["id"],
            title=row["title"],
            updated_at=row["updated_at"],
        ).model_dump(mode="json")
        for row in rows
    ]
    return list_response(
        data=items,
        request_id=request_id,
        trace_id=trace_id,
        total=len(items),
    )


# ---------------------------------------------------------------------------
# GET /{document_id}/backlinks
# ---------------------------------------------------------------------------

@router.get(
    "/{document_id}/backlinks",
    summary="이 문서를 참조하는 문서 목록",
    description=(
        "이 문서를 ``[[제목]]`` 으로 참조하는 출발 문서 목록 (역방향). "
        "viewer Scope 자동 필터 — Scope 밖 출발 문서는 결과에 노출되지 않는다."
    ),
    response_model=SuccessResponse,
    tags=["wikilinks"],
)
def list_backlinks(
    document_id: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    scope_ids = _resolve_viewer_scope_profile_ids(actor)

    with get_db() as conn:
        # 도착 문서 자체에 대한 viewer ACL — 못 보면 404 (존재 유출 방지)
        target = documents_repository.get_by_id(conn, document_id)
        if target is None:
            raise not_found_resource("문서", document_id)
        if scope_ids is not None:
            ids_set = set(scope_ids)
            if target.scope_profile_id and target.scope_profile_id not in ids_set:
                raise not_found_resource("문서", document_id)

        rows, total = document_links_repository.list_backlinks(
            conn,
            to_document_id=document_id,
            viewer_scope_profile_ids=scope_ids,
            page=page,
            page_size=page_size,
        )

    items = [
        BacklinkItem(
            link_id=r["link_id"],
            from_document_id=r["from_document_id"],
            from_document_title=r["from_document_title"],
            node_id=r["node_id"],
            raw_text=r["raw_text"],
            created_at=r["created_at"],
        ).model_dump(mode="json")
        for r in rows
    ]
    return list_response(
        data=items,
        request_id=request_id,
        trace_id=trace_id,
        page=page,
        page_size=page_size,
        total=total,
    )


# ---------------------------------------------------------------------------
# GET /{document_id}/links — admin 전용 (디버깅)
# ---------------------------------------------------------------------------

@router.get(
    "/{document_id}/links",
    summary="이 문서가 내보내는 wikilink 목록 (admin 전용)",
    description=(
        "이 문서의 정방향 wikilink 목록 — resolved/ambiguous/missing 상태 포함. "
        "ORG_ADMIN / SUPER_ADMIN 만 호출 가능 (디버깅 용)."
    ),
    response_model=SuccessResponse,
    tags=["wikilinks"],
)
def list_outgoing_links(
    document_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="admin.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        target = documents_repository.get_by_id(conn, document_id)
        if target is None:
            raise not_found_resource("문서", document_id)
        rows = document_links_repository.list_outgoing(
            conn, from_document_id=document_id, limit=limit,
        )

    items = [
        OutgoingLinkItem(
            id=r.id,
            to_document_id=r.to_document_id,
            node_id=r.node_id,
            raw_text=r.raw_text,
            resolved_status=r.resolved_status,  # type: ignore[arg-type]
            created_at=r.created_at,
        ).model_dump(mode="json")
        for r in rows
    ]
    return list_response(
        data=items,
        request_id=request_id,
        trace_id=trace_id,
        total=len(items),
    )
