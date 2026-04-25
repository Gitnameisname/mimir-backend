"""
Actor 분류 모델.

요청 주체(actor)를 공통 ActorContext로 정규화한다.
라우터 / 서비스 / policy engine이 모두 이 구조를 참조한다.

설계 원칙:
  - anonymous도 정상 actor 상태이다 (실패 상태가 아님).
  - actor_type은 이후 policy engine에서 재사용된다.
  - auth_method는 "어떤 입력으로 식별되었는가" 기록 수준이며,
    검증이 불완전한 상태에서 강한 보안 의미를 부여하지 않는다.

actor_type 분류:
  - anonymous     : 인증 입력 없음
  - user          : 사람 사용자 (session/bearer/api_key)
  - agent         : AI 에이전트 (S2 Phase 4 — API key 또는 OAuth client-credentials)
  - service       : 내부 서비스 caller (internal_service header)

auth_method 값:
  - session           : 세션 쿠키
  - bearer            : Bearer 토큰 (JWT 등)
  - api_key           : X-API-Key 헤더
  - internal_service  : X-Service-Token 헤더 (서비스 간 호출)
  - None              : anonymous (입력 없음)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ActorType(str, Enum):
    ANONYMOUS = "anonymous"
    USER = "user"
    AGENT = "agent"      # S2 Phase 4: AI 에이전트 Principal
    SERVICE = "service"


class AuthMethod(str, Enum):
    SESSION = "session"
    BEARER = "bearer"
    API_KEY = "api_key"
    INTERNAL_SERVICE = "internal_service"


@dataclass
class ActorContext:
    """요청 주체 컨텍스트.

    Attributes:
        actor_type:        anonymous / user / agent / service
        actor_id:          식별된 경우의 사용자/서비스/에이전트 ID (미검증 시 None)
        is_authenticated:  실제 검증 성공 여부
        auth_method:       어떤 입력 소스로 식별 시도했는지
        tenant_id:         멀티테넌시 scope (미검증 시 None)
        role:              전역 역할명 (VIEWER / AUTHOR / ...)  인증 미확인 시 None
        agent_id:          에이전트 UUID (actor_type=AGENT 전용)
        scope_profile_id:  바인딩된 ScopeProfile UUID. 에이전트(S2 Phase 4)와 사용자
                           (S2-5, 2026-04-20 추가) 모두 사용. 값이 없으면 S2 ⑥ 대상
                           리소스(골든셋·평가 Admin 등) 접근 시 403.
        acting_on_behalf_of: 위임 대상 User ID (에이전트 위임 호출 시)
    """

    actor_type: ActorType
    actor_id: Optional[str]
    is_authenticated: bool
    auth_method: Optional[AuthMethod]
    tenant_id: Optional[str]
    role: Optional[str] = None
    # S2 Phase 4: 에이전트 전용 필드 + S2-5 (2026-04-20): scope_profile_id 는 사용자도 사용
    agent_id: Optional[str] = field(default=None)
    scope_profile_id: Optional[str] = field(default=None)
    acting_on_behalf_of: Optional[str] = field(default=None)  # User ID (위임)

    @property
    def is_anonymous(self) -> bool:
        return self.actor_type == ActorType.ANONYMOUS

    @property
    def is_service(self) -> bool:
        return self.actor_type == ActorType.SERVICE

    @property
    def is_agent(self) -> bool:
        return self.actor_type == ActorType.AGENT

    @property
    def resolved_id(self) -> Optional[str]:
        """인증된 경우 actor_id, 미인증이면 None."""
        return self.actor_id if self.is_authenticated else None

    @property
    def audit_actor_type(self) -> str:
        """감사 로그용 actor_type 문자열 (S2 원칙 ⑥).

        도서관 §1.6 BE-G4 (2026-04-25): 매핑을 ``app.utils.actor.actor_type_str``
        에 위임. 시맨틱 통일 — ``ActorType.SERVICE`` → ``"system"`` (이전엔
        ``"service"`` 를 그대로 반환해 ``AuditEmitter`` 의
        ``Literal["user", "agent", "system"]`` 위반 가능). ``ActorType.ANONYMOUS``
        → ``"user"`` (감사 로그 기본값).

        지연 import 로 순환 참조 회피 (utils → models → ...).
        """
        from app.utils.actor import actor_type_str

        return actor_type_str(self)
