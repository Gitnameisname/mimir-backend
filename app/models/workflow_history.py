"""
WorkflowHistory 도메인 모델 (Phase 5).

문서 버전의 모든 상태 변화를 immutable 로그로 추적한다.
ReviewAction과 유사하지만, 이력 조회/감사 전용으로 설계된 별도 엔티티.

Attributes:
    id            : UUID
    document_id   : 소속 문서 UUID
    version_id    : 대상 버전 UUID
    from_status   : 전이 이전 상태
    to_status     : 전이 이후 상태
    action        : 수행된 액션 (WorkflowAction)
    actor_id      : 수행자 actor_id
    comment       : 처리 메모 (optional)
    reason        : 사유 (optional)
    created_at    : 기록 시각 (immutable)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class WorkflowHistory:
    id: str
    document_id: str
    version_id: str
    from_status: str       # WorkflowStatus 값
    to_status: str         # WorkflowStatus 값
    action: str            # WorkflowAction 값
    actor_id: Optional[str]
    actor_role: Optional[str]  # 행위 시점 역할 스냅샷 (Task 5-7)
    comment: Optional[str]
    reason: Optional[str]
    created_at: datetime
