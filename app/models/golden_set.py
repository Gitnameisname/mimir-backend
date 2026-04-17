"""
Golden Q&A 도메인 모델 (Pydantic v2 기반) — Phase 7 FG7.1

GoldenSet: Q&A 컬렉션 엔티티
GoldenItem: 개별 Q&A 항목 (4-tuple: question / expected_answer / source_docs / citations)

Phase 2 Citation 5-tuple과 호환되는 SourceRef / Citation5Tuple을 포함한다.
S2 원칙 ⑤ ⑥ 준수: scope_id ACL 필드, created_by/actor_type 감사 필드.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GoldenSetDomain(str, Enum):
    POLICY = "policy"
    REGULATION = "regulation"
    TECHNICAL_GUIDE = "technical_guide"
    MANUAL = "manual"
    FAQ = "faq"
    CUSTOM = "custom"


class GoldenSetStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Reference primitives (Phase 2 호환)
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    """기대 근거 문서 참조."""
    document_id: str
    version_id: str
    node_id: str


class Citation5Tuple(BaseModel):
    """Phase 2 Citation과 동일 구조 (5-tuple).

    span_offset은 Phase 2와 같이 단일 int(청크 내 시작 오프셋) 또는 None.
    """
    document_id: str
    version_id: str
    node_id: str
    span_offset: Optional[int] = Field(None, ge=0)
    content_hash: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

class GoldenItem(BaseModel):
    """개별 Golden Q&A 항목."""
    id: str
    golden_set_id: str
    version: int = Field(default=1, ge=1)

    question: str = Field(..., min_length=1, max_length=2000)
    expected_answer: str = Field(..., min_length=1, max_length=5000)
    expected_source_docs: list[SourceRef] = Field(default_factory=list)
    expected_citations: list[Citation5Tuple] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)

    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: Optional[str] = None

    @field_validator("expected_source_docs")
    @classmethod
    def require_at_least_one_source(cls, v: list[SourceRef]) -> list[SourceRef]:
        if len(v) == 0:
            raise ValueError("expected_source_docs must contain at least one reference")
        return v


class GoldenSet(BaseModel):
    """Golden Q&A 컬렉션."""
    id: str
    scope_id: str  # S2 ⑥: Scope Profile ID (ACL 기준)

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    domain: GoldenSetDomain = Field(default=GoldenSetDomain.CUSTOM)
    status: GoldenSetStatus = Field(default=GoldenSetStatus.DRAFT)
    version: int = Field(default=1, ge=1)

    items: Optional[list[GoldenItem]] = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: Optional[str] = None

    deleted_at: Optional[datetime] = None
    is_deleted: bool = False


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------

class GoldenItemCreateRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    expected_answer: str = Field(..., min_length=1, max_length=5000)
    expected_source_docs: list[SourceRef] = Field(..., min_length=1)
    expected_citations: list[Citation5Tuple] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)


class GoldenItemUpdateRequest(BaseModel):
    question: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    expected_answer: Optional[str] = Field(default=None, min_length=1, max_length=5000)
    expected_source_docs: Optional[list[SourceRef]] = Field(default=None, min_length=1)
    expected_citations: Optional[list[Citation5Tuple]] = None
    notes: Optional[str] = Field(default=None, max_length=1000)


class GoldenItemResponse(BaseModel):
    id: str
    golden_set_id: str
    version: int
    question: str
    expected_answer: str
    expected_source_docs: list[SourceRef]
    expected_citations: list[Citation5Tuple]
    notes: Optional[str]
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: Optional[str]


class GoldenSetCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    domain: GoldenSetDomain = Field(default=GoldenSetDomain.CUSTOM)
    extra_metadata: dict[str, Any] = Field(default_factory=dict)


class GoldenSetUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    domain: Optional[GoldenSetDomain] = None
    status: Optional[GoldenSetStatus] = None
    extra_metadata: Optional[dict[str, Any]] = None


class GoldenSetResponse(BaseModel):
    id: str
    scope_id: str
    name: str
    description: Optional[str]
    domain: str
    status: str
    version: int
    item_count: Optional[int] = None
    extra_metadata: dict[str, Any]
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: Optional[str]
    is_deleted: bool


class GoldenSetDetailResponse(GoldenSetResponse):
    items: list[GoldenItemResponse] = Field(default_factory=list)


class GoldenSetVersionInfo(BaseModel):
    version: int
    created_at: datetime
    created_by: str
    item_count: int


class GoldenSetVersionDiff(BaseModel):
    from_version: int
    to_version: int
    items_added: list[str] = Field(default_factory=list)
    items_modified: list[str] = Field(default_factory=list)
    items_deleted: list[str] = Field(default_factory=list)
    modified_at: datetime
