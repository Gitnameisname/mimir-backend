"""Annotations 라우터 — S3 Phase 3 FG 3-3.

엔드포인트:
    POST   /api/v1/documents/{document_id}/annotations  — 신규 주석 / 답글
    GET    /api/v1/documents/{document_id}/annotations  — 목록 (include_resolved/include_orphans)
    GET    /api/v1/annotations/{annotation_id}          — 단건
    PATCH  /api/v1/annotations/{annotation_id}          — 본문 수정 (작성자 본인만)
    POST   /api/v1/annotations/{annotation_id}/resolve  — 해결 (작성자 또는 admin)
    POST   /api/v1/annotations/{annotation_id}/reopen   — 재오픈 (작성자 또는 admin)
    DELETE /api/v1/annotations/{annotation_id}          — 삭제 (작성자 또는 admin, cascade 답글)

ACL:
    - 모든 엔드포인트가 documents_service.get_document(actor=actor) 로 ACL 통과 검증.
    - viewer 가 문서를 못 보면 404 (존재 유출 방지).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.rate_limit import limiter
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.models.annotation import Annotation
from app.schemas.annotation import (
    AnnotationCreateRequest,
    AnnotationResponse,
    AnnotationUpdateRequest,
)
from app.services.annotations_service import annotations_service

# 본 라우터는 두 prefix 로 분리 등록되므로 별도 두 router 인스턴스 사용 (registration 측에서 결정)
documents_annotations_router = APIRouter()
annotations_router = APIRouter()

# S3 Phase 6 FG 6-1 (2026-05-18): Rate limit 일괄 — 쓰기는 30/min, 읽기는 60/min.
# 기존 patterns (citations / users_search / search / rag) 와 동일 dependency.
_ANNOTATION_WRITE_LIMIT = "30/minute"
_ANNOTATION_READ_LIMIT = "60/minute"


def _to_response(a: Annotation) -> AnnotationResponse:
    # S3 Phase 6 FG 6-3 (2026-05-18): 응답 직렬화 시 ANSI/control byte sanitize.
    # DB 원본은 raw 그대로 (R-O3: write 시 raw 보존). read 시에만 정규화.
    from app.utils.content_sanitizer import sanitize_for_response
    return AnnotationResponse(
        id=a.id,
        document_id=a.document_id,
        version_id=a.version_id,
        node_id=a.node_id,
        span_start=a.span_start,
        span_end=a.span_end,
        author_id=a.author_id,
        actor_type=a.actor_type,  # type: ignore[arg-type]
        content=sanitize_for_response(a.content),
        status=a.status,  # type: ignore[arg-type]
        resolved_at=a.resolved_at,
        resolved_by=a.resolved_by,
        parent_id=a.parent_id,
        is_orphan=a.is_orphan,
        orphaned_at=a.orphaned_at,
        created_at=a.created_at,
        updated_at=a.updated_at,
        mentioned_user_ids=list(a.mentioned_user_ids or []),
    )


# ---------------------------------------------------------------------------
# /documents/{document_id}/annotations
# ---------------------------------------------------------------------------

@documents_annotations_router.post(
    "/{document_id}/annotations",
    summary="신규 주석 또는 답글 생성",
    response_model=SuccessResponse,
    status_code=201,
)
@limiter.limit(_ANNOTATION_WRITE_LIMIT)
def create_annotation(
    document_id: str,
    body: AnnotationCreateRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        ann = annotations_service.create_annotation(
            conn,
            actor=actor,
            document_id=document_id,
            node_id=body.node_id,
            content=body.content,
            span_start=body.span_start,
            span_end=body.span_end,
            parent_id=body.parent_id,
            version_id=body.version_id,
            # S3 Phase 5 FG 5-5 (2026-05-14): typeahead 명시 user_ids. service 가 scope 검증.
            explicit_mentioned_user_ids=body.mentioned_user_ids,
        )
    return success_response(
        data=_to_response(ann).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@documents_annotations_router.get(
    "/{document_id}/annotations",
    summary="문서의 주석 목록",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_READ_LIMIT)
def list_annotations(
    document_id: str,
    request: Request,
    include_resolved: bool = Query(
        default=True, description="resolved 주석 포함 여부",
    ),
    include_orphans: bool = Query(
        default=True, description="orphan 주석 포함 여부",
    ),
    limit: int = Query(default=200, ge=1, le=500),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        items = annotations_service.list_for_document(
            conn,
            actor=actor,
            document_id=document_id,
            include_resolved=include_resolved,
            include_orphans=include_orphans,
            limit=limit,
        )
    return list_response(
        items=[_to_response(a).model_dump() for a in items],
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# /annotations/{annotation_id}
# ---------------------------------------------------------------------------

@annotations_router.get(
    "/{annotation_id}",
    summary="주석 단건",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_READ_LIMIT)
def get_annotation(
    annotation_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        ann = annotations_service.get(
            conn, actor=actor, annotation_id=annotation_id,
        )
    return success_response(
        data=_to_response(ann).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@annotations_router.patch(
    "/{annotation_id}",
    summary="주석 본문 수정 (작성자 본인만)",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_WRITE_LIMIT)
def update_annotation(
    annotation_id: str,
    body: AnnotationUpdateRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        ann = annotations_service.update_content(
            conn,
            actor=actor,
            annotation_id=annotation_id,
            new_content=body.content,
        )
    return success_response(
        data=_to_response(ann).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@annotations_router.post(
    "/{annotation_id}/resolve",
    summary="주석 해결 (작성자 또는 admin)",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_WRITE_LIMIT)
def resolve_annotation(
    annotation_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        ann = annotations_service.resolve(
            conn, actor=actor, annotation_id=annotation_id,
        )
    return success_response(
        data=_to_response(ann).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@annotations_router.post(
    "/{annotation_id}/reopen",
    summary="주석 재오픈 (작성자 또는 admin)",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_WRITE_LIMIT)
def reopen_annotation(
    annotation_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        ann = annotations_service.reopen(
            conn, actor=actor, annotation_id=annotation_id,
        )
    return success_response(
        data=_to_response(ann).model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


@annotations_router.delete(
    "/{annotation_id}",
    summary="주석 삭제 (cascade 답글)",
    response_model=SuccessResponse,
)
@limiter.limit(_ANNOTATION_WRITE_LIMIT)
def delete_annotation(
    annotation_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        annotations_service.delete(
            conn, actor=actor, annotation_id=annotation_id,
        )
    return success_response(
        data={"id": annotation_id, "deleted": True},
        request_id=request_id,
        trace_id=trace_id,
    )
