"""Contributors 도메인 모델 — S3 Phase 3 FG 3-1.

문서 한 건의 작성자 / 편집자 / 승인자 / 최근 열람자 4 카테고리를
한 번에 반환하기 위한 dataclass 묶음.

원칙:
    - 정보 출처는 기존 자료 재조합 (audit_events + workflow_history + documents.created_by).
    - 신규 추적 로그 도입 없음.
    - actor_type 4 분류는 audit_events.actor_type Literal 과 일치 ('user', 'agent', 'system').
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

ContributorActorType = Literal["user", "agent", "system"]


@dataclass(frozen=True)
class Contributor:
    """단일 contributor 엔트리.

    Attributes:
        actor_id          : audit_events.actor_user_id 또는 documents.created_by 의 raw id.
                            user 의 경우 user.id (UUID 문자열). agent / system 은 도메인 문자열.
        display_name      : 표시용 이름. users.display_name 우선,
                            누락 시 "(알 수 없는 사용자)" / "Mimir 시스템" / "에이전트" 등 placeholder.
        actor_type        : 'user' | 'agent' | 'system'.
        last_activity_at  : 이 카테고리에서의 최근 활동 시각 (UTC, timezone-aware).
                            creator 는 documents.created_at, 그 외는 MAX(occurred_at) 또는 MAX(created_at).
        role_badge        : 사용자의 role_name (예: 'AUTHOR', 'REVIEWER'). 없으면 None.
    """

    actor_id: str
    display_name: str
    actor_type: ContributorActorType
    last_activity_at: Optional[datetime]
    role_badge: Optional[str] = None


@dataclass(frozen=True)
class ContributorsBundle:
    """문서 한 건의 4 카테고리 contributor 묶음.

    Attributes:
        creator   : documents.created_by 기반 단일 contributor (없으면 None — 시스템 생성 등 케이스).
        editors   : audit_events 의 편집 이벤트로부터 distinct actor 목록.
                    creator 와의 중복은 service 계층에서 제외 후 반환.
        approvers : workflow_history.to_status='published' 의 distinct actor 목록.
        viewers   : audit_events.event_type='document.viewed' 의 distinct actor 목록.
                    응답에서 viewers 가 노출되지 않을 수 있다 (FG 3-2 정책 게이트가 결정).
        viewers_included : True 면 viewers 가 응답에 포함, False 면 정책에 의해 제거됨.
                           Service 가 호출자의 include_viewers 의도와 정책 게이트 결과를 결합해 결정.
    """

    creator: Optional[Contributor]
    editors: list[Contributor] = field(default_factory=list)
    approvers: list[Contributor] = field(default_factory=list)
    viewers: list[Contributor] = field(default_factory=list)
    viewers_included: bool = False
