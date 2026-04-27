"""ActorContext → 감사 로그용 actor_type 매핑 + 권한 가드.

본 모듈은 ``docs/함수도서관/backend.md`` §1.6 BE-G4 + §1.10 BE-G6 에 등록된 공통 유틸이다.

제공 함수:
    - :func:`actor_type_str` — ``ActorContext`` 4분류 → AuditEmitter Literal 3분류 매핑 (BE-G4).
    - :func:`require_actor_id` — 인증된 actor 의 ``actor_id`` 를 보장 (BE-G6).
    - :func:`require_admin` — admin 권한 가드 (BE-G6).

상수:
    - :data:`ADMIN_ROLES` — admin 으로 간주되는 role 이름 frozenset (BE-G6).

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
from app.api.errors.exceptions import ApiPermissionDeniedError, ApiValidationError

__all__ = [
    "AuditActorType",
    "actor_type_str",
    # BE-G6 (2026-04-25):
    "ADMIN_ROLES",
    "require_actor_id",
    "require_admin",
]


# ===========================================================================
# BE-G6 (2026-04-25) — 권한 가드
# ===========================================================================

# proposal_queue + scope_profiles 등에서 반복되던 `_ADMIN_ROLES = {"ORG_ADMIN", "SUPER_ADMIN"}`
# 모듈 상수의 단일 진실점. frozenset 으로 immutable 보장.
ADMIN_ROLES: frozenset[str] = frozenset({"ORG_ADMIN", "SUPER_ADMIN"})


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


def require_actor_id(
    actor: ActorContext | None,
    *,
    label: str = "사용자",
) -> str:
    """인증된 actor 의 ``actor_id`` 를 보장하고 반환한다.

    folders_service / collections_service 의 `_require_actor` 보일러를 통합한
    단일 진입점. label 인자로 도메인 메시지 (예: "폴더", "컬렉션") 를 명시.

    :param actor: ``ActorContext`` 또는 ``None``. ``None`` / ``actor_id`` 미설정 시
        ``ApiValidationError`` 를 던진다.
    :param label: 에러 메시지에 들어갈 도메인 라벨 (기본 ``"사용자"``).
    :returns: ``str(actor.actor_id)`` — 인증된 사용자의 정규화 id.
    :raises ApiValidationError: actor 가 None 이거나 actor_id 가 None 일 때.
        메시지 포맷: ``"인증된 {label}만 ... 관리할 수 있습니다"`` 와 호환되도록
        ``f"인증된 {label}만 작업을 수행할 수 있습니다"`` 형태.

    >>> from app.api.auth.models import ActorContext, ActorType, AuthMethod
    >>> u = ActorContext(actor_type=ActorType.USER, actor_id="u1", is_authenticated=True,
    ...                  auth_method=AuthMethod.SESSION, tenant_id=None)
    >>> require_actor_id(u)
    'u1'

    .. note::
        호출자가 직접 메시지를 명시하고 싶으면 본 helper 대신 인라인 검사를 유지.
        본 helper 는 "인증된 {label}만 ..." 메시지 패턴이 적합한 도메인용.
    """
    if actor is None or actor.actor_id is None:
        raise ApiValidationError(f"인증된 {label}만 작업을 수행할 수 있습니다")
    return str(actor.actor_id)


def require_admin(actor: ActorContext) -> None:
    """admin 권한을 검증한다 (보안 가드).

    proposal_queue / scope_profiles 의 `_require_admin` 보일러를 통합. ``actor.role``
    이 :data:`ADMIN_ROLES` 에 속해야 한다.

    :param actor: ``ActorContext`` 인스턴스.
    :raises ApiPermissionDeniedError: 미인증 또는 admin role 이 아닌 경우.
        메시지: ``"관리자 권한이 필요합니다."``

    >>> # admin OK 케이스는 ActorContext 인스턴스 필요 — 테스트 참조

    .. note::
        본 helper 는 단순 role 기반 가드. policy engine 기반 세분화된 권한 검증
        (예: ``authorization_service.authorize(ctx, action, resource)``) 은 별도.
        admin 가드 + 추가 검증이 필요한 라우터는 본 helper 호출 후 정책 검증을
        추가한다.
    """
    if not actor.is_authenticated or actor.role not in ADMIN_ROLES:
        raise ApiPermissionDeniedError("관리자 권한이 필요합니다.")
