"""
Agent 도메인 모델 — Phase 4 (S2).

S2 원칙 ⑤: AI 에이전트는 사람과 동등한 API 소비자.
에이전트는 독립 Principal로 모델링되며, 역할 시스템에 통합된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Agent:
    """AI 에이전트 Principal.

    Attributes:
        id:               에이전트 UUID
        name:             에이전트 식별명
        description:      설명
        organization_id:  소속 조직 UUID
        scope_profile_id: 바인딩된 ScopeProfile UUID (ACL 필터 템플릿)
        is_disabled:      킬스위치 상태 — True면 모든 쓰기 요청 거부
        disabled_at:      킬스위치 활성화 시각
        disabled_reason:  킬스위치 사유
        metadata:         확장 메타데이터 (JSONB)
        created_by:       생성자 User UUID
        created_at:       생성 시각
        updated_at:       수정 시각
    """
    id: str
    name: str
    description: Optional[str]
    organization_id: Optional[str]
    scope_profile_id: Optional[str]
    is_disabled: bool
    disabled_at: Optional[datetime]
    disabled_reason: Optional[str]
    metadata: dict[str, Any]
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime
