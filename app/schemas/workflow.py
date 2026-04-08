"""
Workflow API request/response Pydantic 스키마 (Phase 5).

스키마 목록:
  - WorkflowActionRequest  : 모든 워크플로 액션 공통 요청 바디
  - WorkflowActionResponse : 액션 수행 결과 응답
  - WorkflowHistoryItem    : 이력 단건 응답 아이템
  - ReviewActionItem       : ReviewAction 단건 응답 아이템
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class WorkflowActionRequest(BaseModel):
    """워크플로 액션 요청 공통 바디.

    Fields:
        comment                : 검토 의견 / 처리 메모 (optional)
        reason                 : 변경 사유 (반려 시 강력 권장)
        expected_current_status: 낙관적 동시성 검증용 현재 상태 (optional)
    """

    comment: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="검토 의견 또는 처리 메모",
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="상태 변경 사유 (반려 시 강력 권장)",
    )
    expected_current_status: Optional[str] = Field(
        default=None,
        description=(
            "낙관적 락 보조 필드. 클라이언트가 인식한 현재 상태를 보내면 "
            "서버의 실제 상태와 불일치 시 409를 반환한다."
        ),
    )


class WorkflowActionResponse(BaseModel):
    """워크플로 액션 수행 결과."""

    document_id: str
    version_id: str
    version_number: int
    previous_status: str
    current_status: str
    action: str
    acted_by: Optional[str]
    acted_at: str
    comment: Optional[str] = None
    reason: Optional[str] = None


class WorkflowHistoryItem(BaseModel):
    """워크플로 이력 단건."""

    id: str
    document_id: str
    version_id: str
    from_status: str
    to_status: str
    action: str
    actor_id: Optional[str]
    actor_role: Optional[str] = Field(default=None, description="행위 시점 역할 스냅샷")
    comment: Optional[str]
    reason: Optional[str]
    created_at: datetime


class ReviewActionItem(BaseModel):
    """ReviewAction 단건."""

    id: str
    document_id: str
    version_id: str
    action_type: str
    from_status: str
    to_status: str
    actor_id: Optional[str]
    actor_role: Optional[str] = Field(default=None, description="행위 시점 역할 스냅샷")
    comment: Optional[str]
    reason: Optional[str]
    metadata: dict = Field(default_factory=dict, description="확장 정보")
    created_at: datetime
