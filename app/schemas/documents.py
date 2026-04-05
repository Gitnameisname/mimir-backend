"""
Documents API request/response Pydantic 스키마.

설계 원칙:
  - Request schema: 입력 검증 담당. 플랫폼 규약에 맞는 필드만 노출.
  - Response schema: 외부에 노출할 문서 표현. 이후 versions/nodes embed 없이 유지.
  - metadata: key-value JSONB 확장 구조. null→빈 dict 허용, 64KB 크기 제한.
  - status: DocumentStatus enum (draft 기본값). 이후 workflow 확장 가능 구조.
  - document_type: immutable (create 시 고정). update 요청에서 무시.
  - immutable 필드: id, created_at, created_by, document_type
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

_METADATA_MAX_BYTES = 65_536  # 64 KB


class DocumentStatus(str, Enum):
    """문서 상태 enum.

    이후 Review / PendingApproval 등의 workflow 상태로 확장 가능하다.
    현재는 최소 집합만 열어둔다.
    """

    draft = "draft"
    published = "published"
    archived = "archived"
    deprecated = "deprecated"


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DocumentCreateRequest(BaseModel):
    """POST /documents request body.

    idempotency 후보 endpoint — Task I-9에서 Idempotency-Key 헤더 처리 예정.
    """

    title: str = Field(..., min_length=1, max_length=500, description="문서 제목")
    document_type: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="문서 유형 (예: policy, guide, regulation). create 후 변경 불가.",
    )
    status: DocumentStatus = Field(
        default=DocumentStatus.draft,
        description="초기 문서 상태. 미입력 시 draft.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="확장용 key-value 메타데이터 (JSONB). null → 빈 dict으로 처리.",
    )
    summary: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="문서 요약 (optional).",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("metadata must be a key-value object (dict)")
        serialized = json.dumps(v, ensure_ascii=False).encode()
        if len(serialized) > _METADATA_MAX_BYTES:
            raise ValueError(f"metadata size exceeds {_METADATA_MAX_BYTES // 1024}KB limit")
        return v


class DocumentUpdateRequest(BaseModel):
    """PATCH /documents/{id} request body.

    명시된 필드만 업데이트한다 (partial update).
    - document_type: immutable — 요청에 포함해도 무시.
    - metadata: 전체 replace 정책 (shallow merge 아님).
      None → 수정하지 않음 / {} → 빈 dict으로 교체.
    - 수정 가능 필드: title, status, metadata, summary.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[DocumentStatus] = None
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description="metadata 전체 replace. None이면 기존 값 유지.",
    )
    summary: Optional[str] = Field(None, max_length=2000)

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> Optional[dict[str, Any]]:
        if v is None:
            return None  # None = 수정하지 않음
        if not isinstance(v, dict):
            raise ValueError("metadata must be a key-value object (dict)")
        serialized = json.dumps(v, ensure_ascii=False).encode()
        if len(serialized) > _METADATA_MAX_BYTES:
            raise ValueError(f"metadata size exceeds {_METADATA_MAX_BYTES // 1024}KB limit")
        return v

    def has_updates(self) -> bool:
        """수정할 필드가 하나라도 있는지 확인."""
        return any(
            v is not None for v in [self.title, self.status, self.metadata, self.summary]
        )


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    """문서 단건 응답 표현.

    - versions/nodes 세부 정보는 포함하지 않음 (Task I-8에서 확장).
    - citation-friendly 식별자 확장 슬롯으로 id(UUID)를 그대로 노출.
    - created_by / updated_by: 감사 로그 확장 슬롯 (현재는 actor_id 문자열).
    """

    id: str = Field(description="문서 UUID")
    title: str
    document_type: str = Field(description="문서 유형 (create 후 불변)")
    status: str = Field(description="문서 상태")
    metadata: dict[str, Any] = Field(description="확장 메타데이터")
    summary: Optional[str] = None
    created_by: Optional[str] = Field(None, description="생성자 actor_id")
    updated_by: Optional[str] = Field(None, description="최종 수정자 actor_id")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
