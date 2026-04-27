"""Annotation API request/response 스키마 — S3 Phase 3 FG 3-3."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


AnnotationStatus = Literal["open", "resolved"]
AnnotationActorType = Literal["user", "agent", "system"]


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

class AnnotationCreateRequest(BaseModel):
    node_id: str = Field(description="부착 노드 UUID (Phase 1 안정성)")
    content: str = Field(min_length=1, max_length=10_000)
    span_start: Optional[int] = Field(default=None, ge=0)
    span_end: Optional[int] = Field(default=None, ge=1)
    parent_id: Optional[str] = Field(
        default=None, description="답글일 경우 부모 annotation UUID",
    )
    version_id: Optional[str] = Field(
        default=None, description="작성 시점의 version UUID (옵셔널)",
    )


class AnnotationUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class AnnotationResponse(BaseModel):
    id: str
    document_id: str
    version_id: Optional[str] = None
    node_id: str
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    author_id: str
    actor_type: AnnotationActorType
    content: str
    status: AnnotationStatus
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    parent_id: Optional[str] = None
    is_orphan: bool
    orphaned_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    mentioned_user_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class NotificationResponse(BaseModel):
    id: str
    user_id: str
    kind: str
    payload: dict
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationsMarkReadRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
