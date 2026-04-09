"""
검색 API 요청/응답 Pydantic 스키마.

설계 원칙:
  - SearchQuery: 공통 검색 파라미터 (q, type, status, from_date, to_date, sort, page, limit)
  - DocumentSearchResult: 문서 단위 검색 결과 (snippet 포함)
  - NodeSearchResult: 노드 단위 검색 결과 (breadcrumb + 문서 컨텍스트 포함)
  - SearchResponse: 공통 페이지네이션 응답 래퍼
  - 검색 레이어 추상화: Phase 10 벡터 검색 확장 대비로 SearchEngine 인터페이스 예약
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DocumentSearchQuery(BaseModel):
    """GET /search/documents 쿼리 파라미터."""

    q: str = Field(..., min_length=1, description="검색어 (필수)")
    type: Optional[str] = Field(None, description="DocumentType 필터 (예: POLICY, MANUAL)")
    status: Optional[str] = Field(None, description="문서 상태 필터 (예: published, draft)")
    from_date: Optional[str] = Field(None, description="생성일 시작 (ISO 8601: YYYY-MM-DD)")
    to_date: Optional[str] = Field(None, description="생성일 종료 (ISO 8601: YYYY-MM-DD)")
    sort: Literal["relevance", "created_at", "updated_at"] = Field(
        default="relevance",
        description="정렬 기준: relevance (관련도 순) | created_at | updated_at",
    )
    page: int = Field(default=1, ge=1, description="페이지 번호 (1-based)")
    limit: int = Field(default=20, ge=1, le=100, description="페이지당 결과 수 (최대 100)")


class NodeSearchQuery(BaseModel):
    """GET /search/nodes 쿼리 파라미터."""

    q: str = Field(..., min_length=1, description="검색어 (필수)")
    document_id: Optional[str] = Field(None, description="특정 문서 내 노드 검색 제한")
    type: Optional[str] = Field(None, description="DocumentType 필터")
    sort: Literal["relevance", "created_at"] = Field(default="relevance")
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class DocumentSnippet(BaseModel):
    """검색 결과 스니펫 (ts_headline 기반)."""

    field: str = Field(description="스니펫 출처 필드 (title | summary | content)")
    text: str = Field(description="하이라이팅 포함 스니펫 텍스트 (<b>키워드</b> 마킹)")


class DocumentSearchResult(BaseModel):
    """문서 단위 검색 결과."""

    id: str = Field(description="문서 UUID")
    title: str
    document_type: str
    status: str
    summary: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    current_published_version_id: Optional[str] = None
    # 검색 관련 필드
    rank: float = Field(default=0.0, description="FTS 관련도 점수 (ts_rank)")
    snippets: list[DocumentSnippet] = Field(
        default_factory=list, description="하이라이팅된 스니펫 목록"
    )


class NodeBreadcrumb(BaseModel):
    """노드 위치 경로 항목."""

    node_id: str
    title: Optional[str] = None
    node_type: str


class NodeSearchResult(BaseModel):
    """노드 단위 검색 결과 (문서 컨텍스트 포함)."""

    node_id: str = Field(description="노드 UUID")
    node_type: str
    title: Optional[str] = None
    content_snippet: Optional[str] = Field(None, description="노드 content 스니펫 (하이라이팅)")
    order_index: int
    # 문서 컨텍스트
    document_id: str
    document_title: str
    document_type: str
    document_status: str
    version_id: str
    version_number: int
    # 위치 경로
    breadcrumb: list[NodeBreadcrumb] = Field(
        default_factory=list, description="문서 내 노드 경로 (부모 → 현재)"
    )
    # 검색 관련
    rank: float = Field(default=0.0)


class SearchPagination(BaseModel):
    page: int
    limit: int
    total: int
    has_next: bool


class DocumentSearchResponse(BaseModel):
    """GET /search/documents 응답."""

    query: str
    results: list[DocumentSearchResult]
    pagination: SearchPagination
    search_engine: str = Field(default="postgresql_fts", description="사용된 검색 엔진")


class NodeSearchResponse(BaseModel):
    """GET /search/nodes 응답."""

    query: str
    results: list[NodeSearchResult]
    pagination: SearchPagination
    search_engine: str = Field(default="postgresql_fts")


class UnifiedSearchResponse(BaseModel):
    """GET /search 통합 검색 응답."""

    query: str
    documents: list[DocumentSearchResult]
    nodes: list[NodeSearchResult]
    total_documents: int
    total_nodes: int
    search_engine: str = Field(default="postgresql_fts")


class IndexStatsEntry(BaseModel):
    table_name: str
    total_rows: int
    indexed_rows: int
    unindexed_rows: int


class SearchIndexStats(BaseModel):
    """Admin: 검색 인덱스 현황."""

    stats: list[IndexStatsEntry]
    retrieved_at: datetime
