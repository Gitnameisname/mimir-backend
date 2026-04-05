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
  - service       : 내부 서비스 caller (internal_service header)

auth_method 값:
  - session           : 세션 쿠키
  - bearer            : Bearer 토큰 (JWT 등)
  - api_key           : X-API-Key 헤더
  - internal_service  : X-Service-Token 헤더 (서비스 간 호출)
  - None              : anonymous (입력 없음)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ActorType(str, Enum):
    ANONYMOUS = "anonymous"
    USER = "user"
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
        actor_type:       anonymous / user / service
        actor_id:         식별된 경우의 사용자/서비스 ID (미검증 시 None)
        is_authenticated: 실제 검증 성공 여부
        auth_method:      어떤 입력 소스로 식별 시도했는지
        tenant_id:        멀티테넌시 scope (미검증 시 None)
    """

    actor_type: ActorType
    actor_id: Optional[str]
    is_authenticated: bool
    auth_method: Optional[AuthMethod]
    tenant_id: Optional[str]

    @property
    def is_anonymous(self) -> bool:
        return self.actor_type == ActorType.ANONYMOUS

    @property
    def is_service(self) -> bool:
        return self.actor_type == ActorType.SERVICE
