"""
ChangeLog 도메인 모델 (Phase 5).

모든 문서 변경(편집/상태전이 등)에 대한 사유 기록.
승인/반려뿐 아니라 Draft 수정도 포함할 수 있도록 설계.

Attributes:
    id            : UUID
    document_id   : 소속 문서 UUID
    version_id    : 대상 버전 UUID (optional)
    change_type   : 변경 종류 (예: workflow_transition, content_edit, publish 등)
    reason        : 변경 사유
    actor_id      : 변경자 actor_id
    metadata      : 추가 컨텍스트 (JSONB)
    created_at    : 기록 시각
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ChangeLog:
    id: str
    document_id: str
    change_type: str
    actor_id: Optional[str]
    created_at: datetime
    version_id: Optional[str] = None
    actor_role: Optional[str] = None  # 행위 시점 역할 스냅샷 (Task 5-6)
    reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
