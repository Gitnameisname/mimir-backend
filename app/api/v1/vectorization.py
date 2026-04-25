"""
Vectorization router — /api/v1/vectorization

Phase 10 벡터화 파이프라인 API:

엔드포인트:
  - POST /vectorization/documents/{document_id}            단건 문서 수동 재색인
  - POST /vectorization/documents/{document_id}/versions/{version_id}  특정 버전 수동 재색인
  - POST /vectorization/reindex-all                        전체 플랫폼 재색인 (Admin)
  - POST /vectorization/reindex-by-type                    타입별 재색인 (Admin)
  - GET  /vectorization/stats                              벡터화 현황 통계 (Admin)
  - GET  /vectorization/chunks                             청크 목록 조회 (Admin)
  - POST /vectorization/cleanup                            오래된 청크 cleanup (Admin)
  - GET  /vectorization/token-usage                        임베딩 토큰 사용량 (Admin)
  - POST /vectorization/search/semantic                    벡터 유사도 검색 (내부/테스트용)

재색인 트리거 로직:
  - 문서 Published 상태 전이 시 자동 벡터화: workflow router에서 호출
  - 권한 변경 시 권한 메타데이터만 갱신 (벡터 재계산 없음)

보안:
  - 수동 재색인 / 전체 재색인: SUPER_ADMIN 또는 ORG_ADMIN 권한 필요
  - 단건 문서 재색인: 해당 문서 접근 권한 필요
  - 시맨틱 검색: 일반 인증 사용자
"""

import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_QUERY_LEN = 500  # 임베딩 비용 방어: 500자 이상 쿼리 차단


def _validate_uuid(value: str, field: str) -> None:
    if not _UUID_RE.match(value):
        raise bad_request(f"{field}이(가) 유효한 UUID 형식이 아닙니다.")

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.rate_limit import limiter
from app.api.responses import SuccessResponse, success_response
from app.audit.emitter import audit_emitter
from app.db import get_db
from app.services.vectorization_cooldown import try_acquire as _cooldown_try_acquire, peek_remaining as _cooldown_peek
from app.services.vectorization_service import vectorization_pipeline
from app.services.vectorization_status_service import (
    can_user_reindex,
    get_vectorization_status,
)
from app.utils.http_errors import bad_request, not_found, unprocessable_entity
from app.utils.converters import uuid_str_or_none
from app.repositories.pagination import paginate_page

# 시맨틱 검색 rate limit: 임베딩 API 비용 DoS 방어
_SEMANTIC_SEARCH_LIMIT = "30/minute"
# 재색인 rate limit: OpenAI 대량 호출 방어 (admin 전용이나 추가 방어)
_REINDEX_LIMIT = "5/minute"

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 공통 관리자 권한 확인
# ---------------------------------------------------------------------------

def _require_admin(actor: ActorContext, request: Request) -> None:
    action = (
        "admin.write"
        if request.method in ("POST", "PATCH", "DELETE", "PUT")
        else "admin.read"
    )
    authorization_service.authorize(
        actor=actor,
        action=action,
        resource=ResourceRef(resource_type="admin"),
        require_authenticated=True,
    )


# ---------------------------------------------------------------------------
# FG 0-5 (2026-04-23): Admin OR 작성자 권한 확인
# ---------------------------------------------------------------------------

def _require_admin_or_creator(
    actor: ActorContext,
    conn,
    document_id: str,
) -> None:
    """Admin 이거나 `documents.created_by == actor.user_id` 이면 통과.

    그 외에는 HTTPException(403). documents 부재 시 404.
    """
    if not getattr(actor, "is_authenticated", False):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_by FROM documents WHERE id = %s::uuid",
            (document_id,),
        )
        row = cur.fetchone()
    if not row:
        raise not_found("문서를 찾을 수 없습니다.")
    created_by = row.get("created_by") if isinstance(row, dict) else row[0]

    actor_user_id = getattr(actor, "user_id", None) or getattr(actor, "actor_id", None)
    actor_role = getattr(actor, "role", None)
    if not can_user_reindex(
        actor_user_id=uuid_str_or_none(actor_user_id),
        actor_role=uuid_str_or_none(actor_role),
        document_created_by=uuid_str_or_none(created_by),
    ):
        raise HTTPException(
            status_code=403,
            detail="재벡터화 권한이 없습니다. Admin 또는 문서 작성자만 실행할 수 있습니다.",
        )


# ---------------------------------------------------------------------------
# POST /vectorization/documents/{document_id} — 단건 문서 수동 재색인
# ---------------------------------------------------------------------------

@router.post(
    "/documents/{document_id}",
    summary="단건 문서 수동 재색인 (Admin 또는 작성자)",
    description=(
        "문서를 수동으로 재벡터화한다.\n\n"
        "**권한 (S3 P0 FG 0-5 확장, 2026-04-23)**: Admin(ADMIN/ORG_ADMIN/SUPER_ADMIN) "
        "또는 `documents.created_by == actor.user_id` 인 문서 작성자.\n\n"
        "**쿨다운**: 문서+actor 당 10초. 이내 재요청 시 429 + Retry-After.\n\n"
        "**감사 로그**: `vectorization.reindex_requested` 이벤트를 `actor_type` 과 함께 기록."
    ),
    response_model=SuccessResponse,
    tags=["vectorization"],
)
def reindex_document(
    document_id: str,
    request: Request,
    response: Response,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _validate_uuid(document_id, "document_id")
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        # FG 0-5: Admin 또는 작성자만 통과
        _require_admin_or_creator(actor, conn, document_id)

        # FG 0-5: 쿨다운 — 문서+actor 당 10초
        actor_user_id = getattr(actor, "user_id", None) or getattr(actor, "actor_id", None)
        cool = _cooldown_try_acquire(document_id, uuid_str_or_none(actor_user_id))
        if not cool.acquired:
            response.headers["Retry-After"] = str(max(1, cool.remaining_sec))
            raise HTTPException(
                status_code=429,
                detail=f"재벡터화 쿨다운 중입니다. {cool.remaining_sec}초 후 다시 시도하세요.",
            )

        # Published 버전 확인
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_published_version_id FROM documents WHERE id = %s::uuid",
                (document_id,),
            )
            row = cur.fetchone()

        if not row:
            raise not_found("문서를 찾을 수 없습니다.")

        version_id = row.get("current_published_version_id") if isinstance(row, dict) else row[0]
        if not version_id:
            raise unprocessable_entity("Published 버전이 없어 벡터화할 수 없습니다.")

        job_id = str(uuid.uuid4())
        result = vectorization_pipeline.vectorize_version(
            conn,
            document_id=document_id,
            version_id=str(version_id),
            job_id=job_id,
        )

    # FG 0-5: 감사 이벤트 (actor_type 명시 — S2 원칙 ⑤)
    try:
        _actor_type_raw = getattr(actor, "actor_type", None)
        actor_type_str = (
            getattr(_actor_type_raw, "value", None)
            or (str(_actor_type_raw).lower() if _actor_type_raw else "user")
        )
        # emitter 의 Literal ["user","agent","system"] 에 정규화
        if actor_type_str not in ("user", "agent", "system"):
            actor_type_str = "agent" if actor_type_str in ("service",) else "user"
        audit_emitter.emit(
            event_type="vectorization.reindex_requested",
            action="vectorization.reindex",
            actor_id=uuid_str_or_none(actor_user_id),
            actor_type=actor_type_str,
            resource_type="document",
            resource_id=document_id,
            result="success" if not result.error else "failed",
            request_id=request_id,
        )
    except Exception as exc:
        logger.debug("audit emit failed (non-blocking): %s", exc)

    return success_response(
        data={
            "document_id": document_id,
            "version_id": str(version_id),
            "chunks_created": result.chunks_created,
            "chunks_failed": result.chunks_failed,
            "total_tokens": result.total_tokens,
            "model": result.model,
            "error": result.error,
        },
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# FG 0-5: GET /vectorization/documents/{document_id}/status — 벡터화 상태 조회
# ---------------------------------------------------------------------------

@router.get(
    "/documents/{document_id}/status",
    summary="문서별 벡터화 상태 조회 (FG 0-5, 2026-04-23)",
    description=(
        "문서의 현재 벡터화 상태를 반환한다.\n\n"
        "**status 값**: `indexed | pending | in_progress | failed | stale | not_applicable`\n\n"
        "**권한**: 문서 조회 권한이 있는 인증된 사용자.\n\n"
        "**폐쇄망 호환 (S2 ⑦)**: Milvus / 임베딩 서비스 off 상태에서도 DB 만으로 상태 조회 정상.\n\n"
        "본 엔드포인트는 **읽기 전용** 이며, 재벡터화는 `POST /vectorization/documents/{id}` 를 사용한다."
    ),
    response_model=SuccessResponse,
    tags=["vectorization"],
)
def get_document_vectorization_status(
    document_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    """FG 0-5: 문서 벡터화 상태 + can_reindex 판정 + 쿨다운 잔여 초."""
    _validate_uuid(document_id, "document_id")

    # 인증만 확인. 문서 조회 권한은 Scope Profile + authorization_service 의
    # document.read 정책에 위임 (기존 문서 조회 경로와 동일 기준 권장).
    if not getattr(actor, "is_authenticated", False):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")

    authorization_service.authorize(
        actor=actor,
        action="document.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)

    actor_user_id = getattr(actor, "user_id", None) or getattr(actor, "actor_id", None)
    actor_role = getattr(actor, "role", None)

    with get_db() as conn:
        info = get_vectorization_status(
            conn,
            document_id,
            actor_user_id=uuid_str_or_none(actor_user_id),
            actor_role=uuid_str_or_none(actor_role),
            cooldown_remaining_sec=_cooldown_peek(
                document_id,
                uuid_str_or_none(actor_user_id),
            ),
        )

    if info is None:
        raise not_found("문서를 찾을 수 없습니다.")

    return success_response(
        data=info.to_dict(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /vectorization/documents/{document_id}/versions/{version_id}
# ---------------------------------------------------------------------------

@router.post(
    "/documents/{document_id}/versions/{version_id}",
    summary="특정 버전 수동 재색인",
    response_model=SuccessResponse,
    tags=["vectorization"],
)
def reindex_version(
    document_id: str,
    version_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    _validate_uuid(document_id, "document_id")
    _validate_uuid(version_id, "version_id")
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        job_id = str(uuid.uuid4())
        result = vectorization_pipeline.vectorize_version(
            conn,
            document_id=document_id,
            version_id=version_id,
            job_id=job_id,
        )

    if result.error and result.chunks_created == 0:
        raise HTTPException(status_code=500, detail="벡터화 처리 중 오류가 발생했습니다.")

    return success_response(
        data={
            "document_id": document_id,
            "version_id": version_id,
            "chunks_created": result.chunks_created,
            "chunks_failed": result.chunks_failed,
            "total_tokens": result.total_tokens,
            "model": result.model,
            "error": result.error,
        },
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /vectorization/reindex-all — 전체 재색인 (Admin)
# ---------------------------------------------------------------------------

class ReindexAllRequest(BaseModel):
    document_type: Optional[str] = None
    limit: int = 100


@router.post(
    "/reindex-all",
    summary="전체 플랫폼 재색인 (Admin)",
    response_model=SuccessResponse,
    tags=["vectorization", "admin"],
)
@limiter.limit(_REINDEX_LIMIT)
def reindex_all(
    body: ReindexAllRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        job_id = str(uuid.uuid4())
        result = vectorization_pipeline.vectorize_all_published(
            conn,
            document_type=body.document_type,
            limit=min(body.limit, 500),
            job_id=job_id,
        )

    return success_response(
        data=result,
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /vectorization/stats — 벡터화 현황 통계 (Admin)
# ---------------------------------------------------------------------------

@router.get(
    "/stats",
    summary="벡터화 현황 통계",
    response_model=SuccessResponse,
    tags=["vectorization", "admin"],
)
def get_vectorization_stats(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        with conn.cursor() as cur:
            # 총 청크 수
            cur.execute("""
                SELECT
                    COUNT(*) AS total_chunks,
                    COUNT(*) FILTER (WHERE is_current = TRUE) AS current_chunks,
                    COUNT(*) FILTER (WHERE is_current = TRUE AND embedding IS NOT NULL) AS embedded_chunks,
                    COUNT(*) FILTER (WHERE is_current = TRUE AND embedding IS NULL) AS pending_chunks,
                    COUNT(DISTINCT document_id) FILTER (WHERE is_current = TRUE) AS vectorized_documents
                FROM document_chunks
            """)
            chunk_stats = cur.fetchone()

            # 총 Published 문서 수 (벡터화 대상)
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM documents
                WHERE status = 'published' AND current_published_version_id IS NOT NULL
            """)
            total_docs = cur.fetchone()

            # DocumentType별 청크 수
            cur.execute("""
                SELECT document_type, COUNT(*) AS chunk_count
                FROM document_chunks
                WHERE is_current = TRUE
                GROUP BY document_type
                ORDER BY chunk_count DESC
            """)
            by_type = cur.fetchall()

            # 토큰 사용량 합계
            cur.execute("""
                SELECT
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(chunk_count), 0) AS total_chunks_processed,
                    COUNT(*) AS total_jobs
                FROM embedding_token_usage
            """)
            token_stats = cur.fetchone()

    return success_response(
        data={
            "chunks": {
                "total": chunk_stats["total_chunks"] if chunk_stats else 0,
                "current": chunk_stats["current_chunks"] if chunk_stats else 0,
                "embedded": chunk_stats["embedded_chunks"] if chunk_stats else 0,
                "pending": chunk_stats["pending_chunks"] if chunk_stats else 0,
            },
            "documents": {
                "vectorized": chunk_stats["vectorized_documents"] if chunk_stats else 0,
                "total_published": total_docs["total"] if total_docs else 0,
            },
            "by_type": [
                {"document_type": r["document_type"], "chunk_count": r["chunk_count"]}
                for r in (by_type or [])
            ],
            "token_usage": {
                "total_tokens": token_stats["total_tokens"] if token_stats else 0,
                "total_chunks_processed": token_stats["total_chunks_processed"] if token_stats else 0,
                "total_jobs": token_stats["total_jobs"] if token_stats else 0,
            },
        },
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /vectorization/chunks — 청크 목록 조회 (Admin)
# ---------------------------------------------------------------------------

@router.get(
    "/chunks",
    summary="청크 목록 조회",
    response_model=SuccessResponse,
    tags=["vectorization", "admin"],
)
def list_chunks(
    request: Request,
    document_id: Optional[str] = Query(default=None),
    document_type: Optional[str] = Query(default=None),
    is_current: bool = Query(default=True),
    has_embedding: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    request_id, trace_id = get_request_ids(request)

    if document_id:
        _validate_uuid(document_id, "document_id")

    conditions = ["is_current = %s"]
    params: list = [is_current]

    if document_id:
        conditions.append("document_id = %s::uuid")
        params.append(document_id)
    if document_type:
        conditions.append("document_type = %s")
        params.append(document_type)
    if has_embedding is not None:
        if has_embedding:
            conditions.append("embedding IS NOT NULL")
        else:
            conditions.append("embedding IS NULL")

    where = " AND ".join(conditions)
    page, limit, offset = paginate_page(page, limit)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM document_chunks WHERE {where}", params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"""
                SELECT id, document_id, version_id, node_id, chunk_index,
                       source_text, node_path, document_type, document_status,
                       embedding_model, token_count, is_current,
                       CASE WHEN embedding IS NOT NULL THEN TRUE ELSE FALSE END AS has_embedding,
                       created_at, updated_at
                FROM document_chunks
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "document_id": str(r["document_id"]),
            "version_id": str(r["version_id"]),
            "node_id": str(r["node_id"]) if r.get("node_id") else None,
            "chunk_index": r["chunk_index"],
            "source_text": r["source_text"][:200] + "..." if len(r["source_text"]) > 200 else r["source_text"],
            "node_path": r["node_path"] or [],
            "document_type": r["document_type"],
            "document_status": r["document_status"],
            "embedding_model": r["embedding_model"],
            "token_count": r["token_count"],
            "is_current": r["is_current"],
            "has_embedding": r["has_embedding"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]

    return success_response(
        data={"items": items, "total": total, "page": page, "limit": limit},
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /vectorization/cleanup — 오래된 청크 cleanup (Admin)
# ---------------------------------------------------------------------------

@router.post(
    "/cleanup",
    summary="오래된 청크 물리 삭제 (Admin)",
    response_model=SuccessResponse,
    tags=["vectorization", "admin"],
)
def cleanup_old_chunks(
    request: Request,
    days_old: int = Query(default=30, ge=1, le=365),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        deleted = vectorization_pipeline.cleanup_old_chunks(conn, days_old=days_old)

    return success_response(
        data={"deleted": deleted, "days_old": days_old},
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /vectorization/token-usage — 임베딩 토큰 사용량 (Admin)
# ---------------------------------------------------------------------------

@router.get(
    "/token-usage",
    summary="임베딩 토큰 사용량 조회",
    response_model=SuccessResponse,
    tags=["vectorization", "admin"],
)
def get_token_usage(
    request: Request,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor, request)
    request_id, trace_id = get_request_ids(request)

    page, limit, offset = paginate_page(page, limit)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM embedding_token_usage")
            total = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT id, job_id, document_id, model, total_tokens, chunk_count, created_at
                FROM embedding_token_usage
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r["id"]),
            "job_id": str(r["job_id"]) if r.get("job_id") else None,
            "document_id": str(r["document_id"]) if r.get("document_id") else None,
            "model": r["model"],
            "total_tokens": r["total_tokens"],
            "chunk_count": r["chunk_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]

    return success_response(
        data={"items": items, "total": total, "page": page, "limit": limit},
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST /vectorization/search/semantic — 시맨틱 검색 (내부/테스트용)
# ---------------------------------------------------------------------------

class SemanticSearchRequest(BaseModel):
    q: str = Field(..., min_length=1, max_length=500)
    document_type: Optional[str] = None
    top_k: int = Field(default=10, ge=1, le=50)


@router.post(
    "/search/semantic",
    summary="벡터 유사도 검색",
    response_model=SuccessResponse,
    tags=["vectorization"],
)
@limiter.limit(_SEMANTIC_SEARCH_LIMIT)
def semantic_search(
    body: SemanticSearchRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="search.documents",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)
    actor_role = getattr(actor, "role", None)

    if not body.q or not body.q.strip():
        raise bad_request("검색어를 입력해주세요.")

    with get_db() as conn:
        results = vectorization_pipeline.semantic_search(
            conn,
            query=body.q,
            actor_role=actor_role,
            actor_user_id=getattr(actor, "actor_id", None),
            organization_id=getattr(actor, "tenant_id", None),
            document_type=body.document_type,
            top_k=min(body.top_k, 50),
        )

    return success_response(
        data={"query": body.q, "results": results, "total": len(results)},
        request_id=request_id,
        trace_id=trace_id,
    )
