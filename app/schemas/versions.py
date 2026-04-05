"""
Versions API request/response Pydantic 스키마.

설계 원칙:
  - version create = 새 구조 스냅샷 생성 행위 (diff 계산 아님).
  - version response = 버전 메타 중심. nodes는 별도 endpoint로 분리.
  - version_number: 문서별 순차 증가 (1-based). DB에서 계산.
  - source: manual(기본) / system / import
  - nodes payload: create 시 노드 목록을 함께 받음.
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

_METADATA_MAX_BYTES = 65_536  # 64 KB


class VersionSource(str, Enum):
    manual = "manual"
    system = "system"
    import_ = "import"


class VersionStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


# ---------------------------------------------------------------------------
# Node input (version create 시 함께 전달)
# ---------------------------------------------------------------------------


class NodeCreateItem(BaseModel):
    """버전 생성 시 포함할 노드 입력 항목."""

    node_type: str = Field(
        default="paragraph",
        min_length=1,
        max_length=100,
        description="노드 타입 (paragraph / heading / section / ...)",
    )
    order_index: int = Field(
        default=0,
        ge=0,
        description="형제 노드 간 정렬 인덱스 (0-based)",
    )
    parent_index: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "nodes 배열 내 부모 노드의 인덱스 (0-based). "
            "None이면 최상위 노드. "
            "실제 parent_id는 서비스에서 해석."
        ),
    )
    title: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("metadata must be a key-value object (dict)")
        return v


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    """POST /documents/{document_id}/versions request body.

    idempotency 후보 endpoint — Task I-9에서 Idempotency-Key 헤더 처리 연결.
    """

    label: Optional[str] = Field(None, max_length=200, description="버전 레이블")
    change_summary: Optional[str] = Field(None, description="변경 요약")
    source: VersionSource = Field(
        default=VersionSource.manual,
        description="버전 생성 원인 (manual / system / import)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    nodes: list[NodeCreateItem] = Field(
        default_factory=list,
        description="이 버전에 포함할 노드 목록. 빈 리스트도 허용.",
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


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    """버전 단건/목록 응답.

    - nodes는 포함하지 않음. GET /versions/{id}/nodes 로 별도 조회.
    - citation-friendly: id + document_id + version_number로 고유 식별 가능.
    """

    id: str = Field(description="버전 UUID")
    document_id: str = Field(description="소속 문서 UUID")
    version_number: int = Field(description="문서 내 버전 번호 (1-based 순차)")
    label: Optional[str] = None
    status: str
    change_summary: Optional[str] = None
    source: str
    metadata: dict[str, Any]
    created_by: Optional[str] = Field(None, description="생성자 actor_id")
    created_at: datetime

    model_config = {"from_attributes": True}
