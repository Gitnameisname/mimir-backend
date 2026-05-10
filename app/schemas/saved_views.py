"""
Schemas for Saved Views — S3 Phase 2 FG 2-5.

`POST/PATCH/GET /api/v1/saved-views` 의 입출력 정의.

핵심: **`extra="forbid"` 로 화이트리스트 강제**. 임의 키 저장 시 서버가 해석 못해 500
나는 위험을 차단 (task2-5.md §7 R-01).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# layout 어휘 단일 정본 — frontend `features/documents/layout.ts` 의
# DOCUMENT_LAYOUTS 와 정확히 같아야 한다.
SavedViewLayout = Literal["list", "tree", "cards", "graph"]
SavedViewSortField = Literal["created_at", "updated_at", "title"]
SavedViewSortDirection = Literal["asc", "desc"]


class SavedViewSortItem(BaseModel):
    """multi-sort 의 개별 항목."""

    model_config = ConfigDict(extra="forbid")

    field: SavedViewSortField
    direction: SavedViewSortDirection


class SavedViewFilter(BaseModel):
    """저장 가능한 필터 키 화이트리스트.

    임의 키는 거부 (extra="forbid"). 새 필터 키 추가 시 본 모델 + frontend
    `viewToQueryString` 동시 수정 (FG2-5_Pre-flight_갱신 §2.2).
    """

    model_config = ConfigDict(extra="forbid")

    q: Optional[str] = Field(None, max_length=200, description="제목 부분 일치 (ILIKE)")
    document_type: Optional[list[str]] = Field(None, max_length=20)
    status: Optional[list[str]] = Field(
        None, max_length=10, description="WorkflowStatus 어휘"
    )
    tag: Optional[list[str]] = Field(None, max_length=20, description="정규화된 태그명")
    collection: Optional[list[str]] = Field(
        None, max_length=20, description="컬렉션 UUID list"
    )
    folder: Optional[str] = Field(None, description="단일 폴더 UUID")
    include_subfolders: Optional[bool] = None
    created_from: Optional[date] = None
    created_to: Optional[date] = None
    owner_id: Optional[str] = Field(None, description="작성자 UUID")

    @field_validator("tag", mode="before")
    @classmethod
    def _normalize_tags(cls, v):
        """tag 는 항상 소문자 + strip (서버 저장 일관)."""
        if v is None:
            return None
        if not isinstance(v, list):
            return v
        return [t.strip().lower() for t in v if isinstance(t, str) and t.strip()]


class SavedViewCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    filter: SavedViewFilter = Field(default_factory=SavedViewFilter)
    sort: list[SavedViewSortItem] = Field(default_factory=list, max_length=4)
    layout: SavedViewLayout = "list"
    include_tag_nodes: bool = False


class SavedViewUpdateRequest(BaseModel):
    """PATCH — 모든 필드 optional. 명시된 필드만 갱신."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    filter: Optional[SavedViewFilter] = None
    sort: Optional[list[SavedViewSortItem]] = Field(None, max_length=4)
    layout: Optional[SavedViewLayout] = None
    include_tag_nodes: Optional[bool] = None


class SavedViewResponse(BaseModel):
    """GET 단건 / 목록 응답.

    **`owner_id` 는 응답 모델에 없음** — 공유 URL 의 owner 식별 차단
    (task2-5.md §2.1 (5) / §7 R-02). owner 자신의 목록 응답에도 owner_id 미포함
    (acl 단순화 + 마스킹 일관).
    """

    id: str
    name: str
    filter: SavedViewFilter
    sort: list[SavedViewSortItem]
    layout: SavedViewLayout
    include_tag_nodes: bool
    created_at: datetime
    updated_at: datetime
