"""Contributors API request/response 스키마 — S3 Phase 3 FG 3-1."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


ContributorActorType = Literal["user", "agent", "system"]


class ContributorItem(BaseModel):
    """단일 contributor 항목 (응답 직렬화 모델)."""

    actor_id: str = Field(description="users.id (UUID 문자열) 또는 agent/system 도메인 식별자")
    display_name: str = Field(description="표시명. 누락 시 actor_type 별 placeholder")
    actor_type: ContributorActorType = Field(description="user / agent / system")
    last_activity_at: Optional[datetime] = Field(
        default=None,
        description="이 카테고리에서의 최근 활동 시각 (timezone-aware UTC)",
    )
    role_badge: Optional[str] = Field(
        default=None,
        description="사용자 role_name (예: AUTHOR / REVIEWER / ORG_ADMIN). 없으면 null",
    )

    model_config = {"from_attributes": True}


class ContributorsResponse(BaseModel):
    """문서 contributors 4 카테고리 묶음.

    `viewers` 키는 응답에 **선택적으로** 포함된다 (FG 3-2 정책 게이트가 결합되면
    Scope Profile 의 `expose_viewers` 가 false 일 때 키 자체가 응답에서 제거됨).
    """

    creator: Optional[ContributorItem] = Field(
        default=None,
        description="documents.created_by 단일 contributor. 시스템 생성 시 null",
    )
    editors: list[ContributorItem] = Field(
        default_factory=list,
        description="audit_events 의 편집 이벤트들에서 distinct actor (creator 제외)",
    )
    approvers: list[ContributorItem] = Field(
        default_factory=list,
        description="workflow_history.to_status='published' distinct actor",
    )
    viewers: Optional[list[ContributorItem]] = Field(
        default=None,
        description=(
            "audit_events.event_type='document.viewed' distinct actor. "
            "include_viewers=false 또는 정책 게이트가 차단하면 null (응답에서 키 자체 제거)"
        ),
    )
