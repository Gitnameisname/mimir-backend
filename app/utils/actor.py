"""ActorContext → 감사 로그용 actor_type 매핑 유틸.

본 모듈은 ``docs/함수도서관/backend.md`` §1.6 BE-G4 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`actor_type_str` — ``ActorContext`` 의 4분류
      (anonymous/user/agent/service) 를 ``AuditEmitter`` Literal 3분류
      (``"user"``/``"agent"``/``"system"``) 로 매핑.

도입 배경 (보안):
    - ``app.audit.emitter.AuditEmitter.emit`` 의 ``actor_type`` 인자는
      ``Literal["user", "agent", "system"]`` 로 제약 (F-07 시정, 2026-04-18).
    - 그러나 ``ActorContext.audit_actor_type`` 가 ``ActorType.SERVICE`` 일 때
      ``"service"`` 를 반환 — 위 Literal 위반 (런타임 mypy 미검증으로 통과).
    - 인라인 fallback 패턴
      (``actor.actor_type.value if actor.actor_type else "user"``) 이
      여러 라우터에 분산되어 SERVICE 케이스가 일관되게 처리되지 않음.
    - 본 helper 는 매핑을 단일 진입점으로 강제 — SERVICE → ``"system"``,
      ANONYMOUS → ``"user"`` (감사 로그에서 익명도 user 시도로 기록).

매핑 규칙:
    - ``ActorType.USER``      → ``"user"``
    - ``ActorType.AGENT``     → ``"agent"``
    - ``ActorType.SERVICE``   → ``"system"``  (서비스 caller = 시스템 주체)
    - ``ActorType.ANONYMOUS`` → ``"user"``    (감사 로그 기본값)
    - ``actor`` 가 ``None`` 또는 ``actor_type`` 가 누락 → ``"user"`` (방어적)

CONSTITUTION 준수:
    - 제8조 Single Responsibility — actor_type 매핑만 담당.
    - 제10조 Docstring as Agent Contract — 매핑 표를 명시.
    - S2 원칙 ⑥ "AI 에이전트는 사람과 동등한 API 소비자, actor_type 필수 기록".
"""
from __future__ import annotations

from typing import Any, Literal

from app.api.auth.models import ActorContext, ActorType

__all__ = ["AuditActorType", "actor_type_str"]


# AuditEmitter 와 동일한 Literal 타입 (단일 진실점).
# audit/emitter.py 의 ActorType Literal 과 일치해야 한다.
AuditActorType = Literal["user", "agent", "system"]


def actor_type_str(actor: ActorContext | None) -> AuditActorType:
    """``ActorContext`` 를 AuditEmitter ``actor_type`` 으로 매핑한다.

    :param actor: ``ActorContext`` 인스턴스 또는 ``None``.
        ``None`` / ``actor_type`` 누락 시 방어적으로 ``"user"`` 반환.
    :returns: ``"user"`` / ``"agent"`` / ``"system"`` 중 하나.

    >>> from app.api.auth.models import ActorContext, ActorType, AuthMethod
    >>> u = ActorContext(actor_type=ActorType.USER, actor_id="u1", is_authenticated=True,
    ...                  auth_method=AuthMethod.SESSION, tenant_id=None)
    >>> actor_type_str(u)
    'user'
    >>> a = ActorContext(actor_type=ActorType.AGENT, actor_id="ag-1", is_authenticated=True,
    ...                  auth_method=AuthMethod.API_KEY, tenant_id=None)
    >>> actor_type_str(a)
    'agent'
    >>> s = ActorContext(actor_type=ActorType.SERVICE, actor_id="svc", is_authenticated=True,
    ...                  auth_method=AuthMethod.INTERNAL_SERVICE, tenant_id=None)
    >>> actor_type_str(s)
    'system'
    >>> anon = ActorContext(actor_type=ActorType.ANONYMOUS, actor_id=None, is_authenticated=False,
    ...                     auth_method=None, tenant_id=None)
    >>> actor_type_str(anon)
    'user'
    >>> actor_type_str(None)
    'user'
    """
    if actor is None:
        return "user"
    # actor_type 자체가 None 인 경우 (잘못된 인스턴스 방어)
    at: Any = getattr(actor, "actor_type", None)
    if at is None:
        return "user"
    if at == ActorType.AGENT:
        return "agent"
    if at == ActorType.SERVICE:
        return "system"
    # USER + ANONYMOUS + 알 수 없는 값 → "user" (감사 기본값)
    return "user"
