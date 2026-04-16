"""
Citation 역참조 API — Phase 2 FG2.1

엔드포인트:
  GET /citations/{document_id}/versions/{version_id}/nodes/{node_id}/verify
  GET /citations/{document_id}/versions/{version_id}/nodes/{node_id}/content

설계:
  - 권한 없음 / 청크 없음 → 동일하게 404 (존재 여부 노출 방지)
  - actor_role 기반 ACL 필터링 (document_chunks.accessible_roles)
  - 감사 로그에 actor_type 기록 (user / agent)
  - Rate limiting: 익명 60회/분, 인증 300회/분 (DoS / 해시 브루트포스 방어)
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.rate_limit import limiter
from app.db import get_db
from app.schemas.citation import CitationContentResponse, CitationVerifyResponse
from app.services.retrieval.citation_service import CitationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/citations", tags=["citations"])

_ANON_LIMIT = "60/minute"   # 익명: DoS / 해시 열거 방어
_AUTH_LIMIT = "300/minute"  # 인증 사용자: 정상 검증 트래픽 허용


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get(
    "/{document_id}/versions/{version_id}/nodes/{node_id}/verify",
    response_model=CitationVerifyResponse,
    summary="Citation 유효성 검증",
    description=(
        "content_hash를 재계산하여 현재 DB 내용과 일치 여부를 반환한다. "
        "권한 없음 또는 존재하지 않는 Citation → 404."
    ),
)
@limiter.limit(_AUTH_LIMIT)
def verify_citation(
    request: Request,
    document_id: UUID,
    version_id: UUID,
    node_id: UUID,
    content_hash: str = Query(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 hex digest (64자)",
    ),
    span_offset: Optional[int] = Query(None, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    _log_access(actor, "citation.verify", document_id)
    with get_db() as conn:
        svc = CitationService(conn)
        result = svc.verify(
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            content_hash=content_hash,
            actor_role=actor.role if actor else None,
            span_offset=span_offset,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Citation not found")
    return result


@router.get(
    "/{document_id}/versions/{version_id}/nodes/{node_id}/content",
    response_model=CitationContentResponse,
    summary="Citation 원문 조회",
    description=(
        "Citation 좌표에 해당하는 청크 원문과 메타데이터를 반환한다. "
        "권한 없음 또는 존재하지 않는 Citation → 404."
    ),
)
@limiter.limit(_AUTH_LIMIT)
def get_citation_content(
    request: Request,
    document_id: UUID,
    version_id: UUID,
    node_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
):
    _log_access(actor, "citation.content", document_id)
    with get_db() as conn:
        svc = CitationService(conn)
        result = svc.get_content(
            document_id=document_id,
            version_id=version_id,
            node_id=node_id,
            actor_role=actor.role if actor else None,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Citation not found")
    return result


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _log_access(actor: Optional[ActorContext], action: str, document_id: UUID) -> None:
    """감사 로그 기록 — actor_type 필수 포함 (S2 원칙)."""
    actor_type = "anonymous"
    actor_id = "anonymous"
    if actor:
        actor_type = getattr(actor, "actor_type", actor.role or "user")
        actor_id = str(getattr(actor, "actor_id", "?"))

    logger.info(
        "citation_access action=%s actor_id=%s actor_type=%s document_id=%s",
        action,
        actor_id,
        actor_type,
        document_id,
    )
