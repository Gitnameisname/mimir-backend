"""
Versions API request/response Pydantic 스키마.

Phase 4 확장:
  - VersionStatus: draft | published | superseded | discarded
  - DraftSaveRequest: PUT /draft (본문 전체 교체)
  - PublishRequest: POST /publish
  - RestoreRequest: POST /versions/{vid}/restore
  - VersionSummaryResponse: 버전 목록용 (content_snapshot 제외)
  - VersionDetailResponse: 버전 상세용 (content_snapshot + actions 포함)
"""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

_METADATA_MAX_BYTES = 65_536  # 64 KB
_CONTENT_SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


class VersionSource(str, Enum):
    manual = "manual"
    system = "system"
    restore = "restore"


class VersionStatus(str, Enum):
    draft = "draft"
    published = "published"
    superseded = "superseded"
    discarded = "discarded"


# ---------------------------------------------------------------------------
# Node input (version create 시 함께 전달 — 기존 호환 유지)
# ---------------------------------------------------------------------------


class NodeCreateItem(BaseModel):
    """버전 생성 시 포함할 노드 입력 항목."""

    node_type: str = Field(default="paragraph", min_length=1, max_length=100)
    order_index: int = Field(default=0, ge=0)
    parent_index: Optional[int] = Field(default=None, ge=0)
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
    """POST /documents/{id}/versions request body (기존 호환 유지)."""

    label: Optional[str] = Field(None, max_length=200)
    change_summary: Optional[str] = None
    source: VersionSource = Field(default=VersionSource.manual)
    metadata: dict[str, Any] = Field(default_factory=dict)
    nodes: list[NodeCreateItem] = Field(default_factory=list)

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


class DraftSaveRequest(BaseModel):
    """PUT /documents/{id}/draft request body.

    현재 Draft를 전체 교체한다. Draft가 없으면 새로 생성한다.
    content_snapshot은 문서 본문 트리 전체를 나타낸다.
    """

    title: Optional[str] = Field(
        None, min_length=1, max_length=500,
        description="이번 Draft의 제목 스냅샷. None이면 문서 현재 제목 사용.",
    )
    summary: Optional[str] = Field(None, max_length=2000, description="요약 스냅샷")
    label: Optional[str] = Field(None, max_length=200, description="버전 레이블")
    change_summary: Optional[str] = Field(None, description="변경 요약 (사용자 작성)")
    content_snapshot: dict[str, Any] = Field(
        ...,
        description="문서 본문 구조 트리 전체. type='document' 루트를 포함해야 한다.",
    )

    @field_validator("content_snapshot", mode="before")
    @classmethod
    def validate_content_snapshot(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("content_snapshot must be a JSON object")
        serialized = json.dumps(v, ensure_ascii=False).encode()
        if len(serialized) > _CONTENT_SNAPSHOT_MAX_BYTES:
            raise ValueError(
                f"content_snapshot size exceeds {_CONTENT_SNAPSHOT_MAX_BYTES // (1024*1024)}MB limit"
            )
        return v


class PublishRequest(BaseModel):
    """POST /documents/{id}/publish request body."""

    change_summary: Optional[str] = Field(
        None, description="발행 시 변경 요약 (기존 Draft의 change_summary를 덮어쓴다)"
    )


class RestoreRequest(BaseModel):
    """POST /documents/{id}/versions/{vid}/restore request body."""

    change_summary: Optional[str] = Field(
        None, description="복원 이유 또는 설명 (선택)"
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    """버전 목록/단건 기본 응답 (content_snapshot 제외).

    기존 POST /versions 호환 유지 + Phase 4 신규 필드 추가.
    """

    id: str = Field(description="버전 UUID")
    document_id: str
    version_number: int
    label: Optional[str] = None
    status: str
    # Phase 5: 워크플로 상태 (Task 5-8 UI 연동 포인트)
    workflow_status: Optional[str] = Field(
        default=None,
        description="워크플로 상태 (draft/in_review/approved/published/rejected/archived)",
    )
    change_summary: Optional[str] = None
    source: str
    metadata: dict[str, Any]
    created_by: Optional[str] = None
    created_at: datetime
    # Phase 4 확장
    parent_version_id: Optional[str] = None
    restored_from_version_id: Optional[str] = None
    published_by: Optional[str] = None
    published_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class VersionActionsResponse(BaseModel):
    """버전 상세에서 제공하는 가능한 액션 정보."""

    can_restore: bool
    restore_blocked_reason: Optional[str] = None


class VersionDetailResponse(BaseModel):
    """버전 상세 응답 (content_snapshot + is_current_* + actions 포함).

    GET /documents/{id}/versions/{vid} 응답.
    """

    id: str
    document_id: str
    version_number: int
    label: Optional[str] = None
    status: str
    # Phase 5: 워크플로 상태 (Task 5-8 UI 연동)
    workflow_status: Optional[str] = Field(
        default=None,
        description="워크플로 상태 (draft/in_review/approved/published/rejected/archived)",
    )
    change_summary: Optional[str] = None
    source: str
    metadata: dict[str, Any]
    created_by: Optional[str] = None
    created_at: datetime
    parent_version_id: Optional[str] = None
    restored_from_version_id: Optional[str] = None
    title_snapshot: Optional[str] = None
    summary_snapshot: Optional[str] = None
    metadata_snapshot: Optional[dict[str, Any]] = None
    content_snapshot: Optional[dict[str, Any]] = None
    published_by: Optional[str] = None
    published_at: Optional[datetime] = None
    # 현재 활성 여부 플래그
    is_current_draft: bool = False
    is_current_published: bool = False
    # 요청자 기준 가능한 액션
    actions: VersionActionsResponse

    model_config = {"from_attributes": True}


class VersionSummaryResponse(BaseModel):
    """버전 목록용 요약 응답 (is_current_* + can_restore 포함).

    GET /documents/{id}/versions 응답의 각 항목.
    """

    id: str
    document_id: str
    version_number: int
    label: Optional[str] = None
    status: str
    # Phase 5: 워크플로 상태 (Task 5-8 UI 연동 포인트)
    workflow_status: Optional[str] = Field(
        default=None,
        description="워크플로 상태 (draft/in_review/approved/published/rejected/archived)",
    )
    change_summary: Optional[str] = None
    source: str
    created_by: Optional[str] = None
    created_at: datetime
    published_at: Optional[datetime] = None
    published_by: Optional[str] = None
    restored_from_version_id: Optional[str] = None
    is_current_draft: bool = False
    is_current_published: bool = False
    can_restore: bool = False

    model_config = {"from_attributes": True}
