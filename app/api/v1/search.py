"""
Search router — /api/v1/search

Phase 8 검색 API:
  - GET /search/filter-options  검색 필터 옵션 (DocumentType 목록, 공개)
  - GET /search                 통합 검색 (문서 + 노드, 각 최대 5건 미리보기)
  - GET /search/documents       문서 단위 전문 검색
  - GET /search/nodes           노드 단위 전문 검색
  - GET /search/index-stats     인덱스 현황 (Admin)
  - POST /search/reindex        수동 재인덱싱 (Admin)

공통 설계 원칙:
  - Permission-First: 권한 기반 필터링은 항상 강제 적용
  - API-First: UI뿐 아니라 외부 시스템/AI 에이전트가 사용 가능한 인터페이스
  - 검색 레이어 추상화: search_service 내부는 FTS이지만 API 계약은 엔진 독립적
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.rate_limit import limiter
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.services.search_service import search_service

router = APIRouter()

# 검색 엔드포인트 rate limit:
#   - 익명: IP당 30회/분 (DoS 방어)
#   - 인증 사용자: IP당 120회/분 (정상 사용 여유)
_ANON_LIMIT = "30/minute"
_AUTH_LIMIT = "120/minute"

_MAX_QUERY_LEN = 300
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_q(q: str) -> None:
    """검색어 길이 및 기본 유효성 검사."""
    if len(q) > _MAX_QUERY_LEN:
        raise HTTPException(status_code=400, detail=f"검색어는 {_MAX_QUERY_LEN}자를 초과할 수 없습니다.")


def _validate_uuid(value: Optional[str], field: str) -> None:
    if value and not _UUID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{field}이(가) 유효한 UUID 형식이 아닙니다.")


def _validate_date(value: Optional[str], field: str) -> None:
    if value and not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{field}은(는) YYYY-MM-DD 형식이어야 합니다.")


# ---------------------------------------------------------------------------
# GET /search/filter-options — 검색 필터 옵션 (공개)
# ---------------------------------------------------------------------------


@router.get(
    "/filter-options",
    summary="검색 필터 옵션 조회",
    description=(
        "검색 UI에서 사용할 DocumentType 목록 등 필터 옵션을 반환한다.\n\n"
        "- `document_types`: 활성 상태(`ACTIVE`)인 타입 코드 + 표시명 목록\n"
        "- 타입이 추가/비활성화되면 자동 반영됨"
    ),
    response_model=SuccessResponse,
    tags=["search"],
)
@limiter.limit(_ANON_LIMIT)
def get_filter_options(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="search.filter_options",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type_code, display_name FROM document_types WHERE status = 'ACTIVE' ORDER BY display_name"
            )
            rows = cur.fetchall()

    document_types = [
        {"type_code": row["type_code"], "display_name": row["display_name"]}
        for row in rows
    ]
    return success_response(
        data={"document_types": document_types},
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /search — 통합 검색
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="통합 검색",
    description=(
        "문서 + 노드를 함께 검색한다 (각 최대 5건).\n\n"
        "전체 결과가 필요하면 `/search/documents`, `/search/nodes`를 사용한다.\n\n"
        "**권한**: 요청자의 역할에 따라 열람 가능한 문서만 포함된다.\n"
        "- 미인증/VIEWER: `published` 문서만\n"
        "- AUTHOR/REVIEWER/APPROVER: `published` + `draft`\n"
        "- ADMIN: 모든 상태"
    ),
    response_model=SuccessResponse,
    tags=["search"],
)
@limiter.limit(_ANON_LIMIT)
def unified_search(
    request: Request,
    q: str,
    type: Optional[str] = None,
    status: Optional[str] = None,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _validate_q(q)
    authorization_service.authorize(
        actor=actor,
        action="search.query",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)
    actor_role = getattr(actor, "role", None)

    with get_db() as conn:
        result = search_service.search_unified(
            conn, q, doc_type=type, status=status, actor_role=actor_role
        )

    return success_response(data=result.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /search/documents — 문서 단위 검색
# ---------------------------------------------------------------------------


@router.get(
    "/documents",
    summary="문서 검색",
    description=(
        "문서 제목/요약/본문을 전문 검색한다.\n\n"
        "**파라미터**:\n"
        "- `q`: 검색어 (필수)\n"
        "- `type`: DocumentType 필터 (예: POLICY, MANUAL)\n"
        "- `status`: 문서 상태 필터\n"
        "- `from_date`, `to_date`: 생성일 범위 (YYYY-MM-DD)\n"
        "- `sort`: `relevance` | `created_at` | `updated_at`\n"
        "- `page`, `limit`: 페이지네이션\n\n"
        "**응답**: 각 결과에 `snippets` (키워드 하이라이팅 포함) 포함."
    ),
    response_model=SuccessResponse,
    tags=["search"],
)
@limiter.limit(_ANON_LIMIT)
def search_documents(
    request: Request,
    q: str,
    type: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    sort: str = "relevance",
    page: int = 1,
    limit: int = 20,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _validate_q(q)
    _validate_date(from_date, "from_date")
    _validate_date(to_date, "to_date")
    authorization_service.authorize(
        actor=actor,
        action="search.documents",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)
    actor_role = getattr(actor, "role", None)

    with get_db() as conn:
        result = search_service.search_documents(
            conn,
            q,
            doc_type=type,
            status=status,
            from_date=from_date,
            to_date=to_date,
            sort=sort,
            page=max(1, page),
            limit=min(100, max(1, limit)),
            actor_role=actor_role,
        )

    return success_response(data=result.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /search/nodes — 노드 단위 검색
# ---------------------------------------------------------------------------


@router.get(
    "/nodes",
    summary="노드(섹션) 검색",
    description=(
        "문서 내 노드(섹션/단락) 단위로 검색한다.\n\n"
        "결과에 `breadcrumb` (문서 내 위치 경로)와 `content_snippet` (하이라이팅)이 포함된다.\n\n"
        "**파라미터**:\n"
        "- `q`: 검색어\n"
        "- `document_id`: 특정 문서 내 검색 제한 (optional)\n"
        "- `type`: DocumentType 필터\n"
        "- `sort`: `relevance` | `created_at`\n"
        "- `page`, `limit`: 페이지네이션"
    ),
    response_model=SuccessResponse,
    tags=["search"],
)
@limiter.limit(_ANON_LIMIT)
def search_nodes(
    request: Request,
    q: str,
    document_id: Optional[str] = None,
    type: Optional[str] = None,
    sort: str = "relevance",
    page: int = 1,
    limit: int = 20,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _validate_q(q)
    _validate_uuid(document_id, "document_id")
    authorization_service.authorize(
        actor=actor,
        action="search.nodes",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=False,
    )
    request_id, trace_id = get_request_ids(request)
    actor_role = getattr(actor, "role", None)

    with get_db() as conn:
        result = search_service.search_nodes(
            conn,
            q,
            document_id=document_id,
            doc_type=type,
            sort=sort,
            page=max(1, page),
            limit=min(100, max(1, limit)),
            actor_role=actor_role,
        )

    return success_response(data=result.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# GET /search/index-stats — 검색 인덱스 현황 (Admin용)
# ---------------------------------------------------------------------------


@router.get(
    "/index-stats",
    summary="검색 인덱스 현황 조회",
    description=(
        "각 테이블별 인덱싱 현황을 반환한다 (총 rows, 인덱싱 완료, 미완료).\n\n"
        "Admin 대시보드 및 재인덱싱 판단 기준으로 사용한다."
    ),
    response_model=SuccessResponse,
    tags=["search", "admin"],
)
def get_index_stats(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="search.index_stats",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        stats = search_service.get_index_stats(conn)

    return success_response(data=stats.model_dump(), request_id=request_id, trace_id=trace_id)


# ---------------------------------------------------------------------------
# POST /search/reindex — 수동 재인덱싱 (Admin용)
# ---------------------------------------------------------------------------


@router.post(
    "/reindex",
    summary="검색 인덱스 수동 재인덱싱",
    description=(
        "모든 테이블의 search_vector를 일괄 갱신한다.\n\n"
        "인덱스 누락 또는 손상 시 수동 복구용으로 사용한다.\n\n"
        "**주의**: 대량 데이터 환경에서는 시간이 소요될 수 있다."
    ),
    response_model=SuccessResponse,
    tags=["search", "admin"],
)
def reindex(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="search.reindex",
        resource=ResourceRef(resource_type="search"),
        require_authenticated=True,
    )
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        result = search_service.reindex_all(conn)

    return success_response(data=result, request_id=request_id, trace_id=trace_id)
