"""
ReviewAction 도메인 모델 (Phase 5).

문서 버전에 대한 검토/승인/반려 액션 기록.
version 기준으로 독립적으로 관리되며, 동일 문서라도 버전별 review 이력이 분리된다.

Attributes:
    id            : UUID
    document_id   : 소속 문서 UUID
    version_id    : 대상 버전 UUID
    action_type   : 액션 종류 (WorkflowAction)
    from_status   : 전이 이전 상태
    to_status     : 전이 이후 상태
    actor_id      : 수행자 actor_id
    comment       : 검토 의견 (optional)
    reason        : 상태 변경 사유 (optional, 반려 시 권장 필수)
    created_at    : 기록 시각
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ReviewAction:
    id: str
    document_id: str
    version_id: str
    action_type: str       # WorkflowAction 값
    from_status: str       # WorkflowStatus 값
    to_status: str         # WorkflowStatus 값
    actor_id: Optional[str]
    actor_role: Optional[str]  # 행위 시점 역할 스냅샷 (Task 5-5)
    comment: Optional[str]
    reason: Optional[str]
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)  # 확장 정보 (Task 5-5)
