"""
Tags 라우터 — /api/v1/tags — S3 Phase 2 FG 2-2.

엔드포인트:
  GET    /tags?q=<prefix>&limit= — 자동완성 (prefix + usage 빈도)
  GET    /tags/popular?limit=    — 전체 사용 빈도 상위
  DELETE /tags/{id}              — admin 전용 전역 삭제 (document_tags CASCADE)

문서 상세의 태그 목록은 DocumentResponse 확장은 별도 FG — 지금은 태그 전역 풀에만 집중.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db import get_db
from app.models.tag import Tag
from app.repositories.tags_repository import tags_repository
from app.schemas.tags import TagResponse
from app.services.tag_rules import normalize_tag

router = APIRouter()


def _to_response(tag: Tag) -> TagResponse:
    return TagResponse(
        id=tag.id,
        name=tag.name_normalized,
        created_at=tag.created_at,
        usage_count=tag.usage_count,
    )


@router.get(
    "",
    summary="태그 자동완성 (prefix + usage 순)",
    response_model=SuccessResponse,
)
def list_tags(
    request: Request,
    q: str | None = Query(default=None, description="prefix 검색어 (서버에서 정규화 후 매칭)"),
    limit: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="tag.list",
        resource=ResourceRef(resource_type="tag"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)

    # q 는 사용자 입력이므로 서버에서 정규화 (NFKC + lower + [\\w/-]{1,64})
    normalized = normalize_tag(q) if q else None
    # normalize_tag 는 1자 이상 요구. q 가 빈 문자열이면 null 로 처리해 popular 와 동등
    prefix = normalized if normalized else None

    with get_db() as conn:
        tags = tags_repository.search_prefix(conn, q=prefix, limit=limit)
    return list_response(
        data=[_to_response(t).model_dump() for t in tags],
        total=len(tags),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get(
    "/popular",
    summary="전체 사용 빈도 상위 태그",
    response_model=SuccessResponse,
)
def list_popular_tags(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    min_usage: int = Query(default=1, ge=1, le=1000),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="tag.list",
        resource=ResourceRef(resource_type="tag"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        tags = tags_repository.popular(conn, limit=limit, min_usage=min_usage)
    return list_response(
        data=[_to_response(t).model_dump() for t in tags],
        total=len(tags),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.delete(
    "/{tag_id}",
    status_code=204,
    summary="태그 삭제 (admin 전용)",
)
def delete_tag(
    tag_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> None:
    authorization_service.authorize(
        actor=actor,
        action="tag.delete",
        resource=ResourceRef(resource_type="tag", resource_id=tag_id),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    with get_db() as conn:
        tags_repository.delete(conn, tag_id)
    audit_emitter.emit_for_actor(
        event_type="tag.deleted",
        action="tag.delete",
        actor=actor,
        resource_type="tag",
        resource_id=tag_id,
        request_id=request_id,
        trace_id=trace_id,
    )
