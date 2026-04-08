"""
Actor extraction dependency — resolve_current_actor.

FastAPI dependency로 사용하며, 요청에서 actor를 추출해
ActorContext로 정규화하고 RequestContext.actor를 갱신한다.

책임 범위:
  - 인증 입력 소스(헤더/쿠키)를 확인해 actor_type 결정
  - anonymous / user / service 중 하나로 정규화
  - X-Actor-Id / X-Actor-Role 개발용 헤더로 actor 주입 지원
  - RequestContext.actor에 반영
  - ActorContext 반환 (downstream에서 사용)

이 dependency는 "누가 요청했는가"만 정규화한다.
"이 actor가 이 action을 할 수 있는가"는 AuthorizationService가 판단한다.

인증 입력 소스 우선순위:
  1. X-Service-Token  → service actor (내부 서비스 간 호출)
  2. Authorization: Bearer <token>  → user actor (JWT 등, Phase 8 연동 예정)
  3. X-API-Key  → user actor (API key, DB 조회 포함)
  4. Cookie: session=<token>  → user actor (세션)
  5. 없음  → anonymous

개발용 헤더 (Phase 8 이전 임시):
  - X-Actor-Id   : actor_id 직접 주입
  - X-Actor-Role : actor role 직접 주입 (VIEWER/AUTHOR/REVIEWER/APPROVER/ORG_ADMIN/SUPER_ADMIN)
  위 두 헤더가 모두 있으면 is_authenticated=True로 처리.

TODO:
  - X-Service-Token 실제 검증 연결 예정 (internal_service auth)
  - Bearer token JWT verifier 연결 예정 (Phase 8)
  - session resolver (Redis/DB) 연결 예정 (Phase 8)
"""

import logging

from fastapi import Request

from app.api.auth.models import ActorContext, ActorType, AuthMethod
from app.config import settings

logger = logging.getLogger(__name__)

_VALID_ROLES = {"VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"}


def _lookup_role_from_db(actor_id: str) -> str | None:
    """users 테이블에서 actor_id로 role_name을 조회한다.

    인증은 완료됐지만 role이 헤더/토큰에 포함되지 않은 경우 사용.
    조회 실패 시 None 반환 (caller에서 폴백 처리).
    """
    try:
        from app.db.connection import get_db
        from app.repositories.users_repository import users_repository
        with get_db() as conn:
            user = users_repository.get_by_id(conn, actor_id)
            return user.role_name if user else None
    except Exception as exc:
        logger.warning("role lookup failed for actor %s: %s", actor_id, exc)
        return None


def resolve_current_actor(request: Request) -> ActorContext:
    """요청에서 actor를 추출하여 ActorContext로 정규화한다.

    RequestContextMiddleware가 먼저 실행되어 request.state.context가
    세팅된 상태를 전제한다. context가 없는 경우(테스트 등)에도 동작한다.

    Returns:
        ActorContext: 정규화된 actor. downstream에서 Depends()로 주입받아 사용.
    """
    actor = _extract_actor(request)

    # RequestContext.actor 갱신 — context 미들웨어가 없는 환경에서도 안전하게
    if hasattr(request.state, "context"):
        request.state.context.actor = actor

    return actor


# ---------------------------------------------------------------------------
# 내부 구현 — 인증 입력 소스별 actor 추출
# ---------------------------------------------------------------------------


def _extract_actor(request: Request) -> ActorContext:
    """인증 입력 소스 우선순위에 따라 ActorContext를 반환한다."""

    # 1. internal service header
    service_token = request.headers.get("X-Service-Token")
    if service_token:
        return _extract_service_actor(service_token)

    # 2. bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return _extract_bearer_actor(token, request)

    # 3. api key (DB 조회로 user_id, role 채움)
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return _extract_api_key_actor(api_key)

    # 4. session cookie
    session_token = request.cookies.get("session")
    if session_token:
        return _extract_session_actor(session_token)

    # 5. 개발용 헤더 (X-Actor-Id + X-Actor-Role) — production 환경에서는 차단
    if settings.environment != "production":
        actor_id = request.headers.get("X-Actor-Id")
        actor_role = request.headers.get("X-Actor-Role")
        if actor_id and actor_role:
            return _extract_dev_header_actor(actor_id, actor_role)

    # 6. anonymous
    return _anonymous_actor()


def _extract_service_actor(token: str) -> ActorContext:
    """내부 서비스 토큰으로 service actor를 반환한다.

    현재는 토큰 존재만 확인. is_authenticated=True로 설정해
    authorize() 1단계(SERVICE actor 전체 허용)가 올바르게 작동하도록 한다.

    TODO: X-Service-Token 실제 검증 로직 연결 예정 (Phase 8).
    """
    return ActorContext(
        actor_type=ActorType.SERVICE,
        actor_id=None,
        is_authenticated=True,   # 토큰 존재 = 인증된 내부 서비스로 처리
        auth_method=AuthMethod.INTERNAL_SERVICE,
        tenant_id=None,
        role=None,
    )


def _extract_bearer_actor(token: str, request: Request) -> ActorContext:
    """Bearer 토큰으로 user actor를 반환한다.

    Phase 8 이전: X-Actor-Id 헤더로 actor_id를 주입.
    role 우선순위:
      1. X-Actor-Role 헤더 (개발 편의용)
      2. users 테이블 DB 조회 (actor_id 있을 때)
    TODO: JWT verifier 연결 예정 (Phase 8) — token 자체 검증 후 actor_id 추출.
    """
    actor_id = request.headers.get("X-Actor-Id") or None
    actor_role = request.headers.get("X-Actor-Role")
    role: str | None = actor_role if actor_role in _VALID_ROLES else None

    # role이 헤더에 없고 actor_id를 알고 있으면 DB에서 조회
    if actor_id and not role:
        role = _lookup_role_from_db(actor_id)

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=bool(actor_id),
        auth_method=AuthMethod.BEARER,
        tenant_id=None,
        role=role,
    )


def _extract_api_key_actor(api_key: str) -> ActorContext:
    """API key로 user actor를 반환한다.

    api_keys 테이블에서 key_hash 조회 → user_id, role 채움.
    TODO: key_hash 검증 로직 완성 (Phase 8).
    """
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                # key_prefix로 후보 조회 후 hash 검증 (현재는 prefix 매칭만)
                prefix = api_key[:8] if len(api_key) >= 8 else api_key
                cur.execute(
                    """
                    SELECT ak.issuer_id, u.role_name
                    FROM api_keys ak
                    LEFT JOIN users u ON u.id::text = ak.issuer_id
                    WHERE ak.key_prefix = %s AND ak.status = 'ACTIVE'
                      AND (ak.expires_at IS NULL OR ak.expires_at > NOW())
                    LIMIT 1
                    """,
                    (prefix,),
                )
                row = cur.fetchone()
                if row and row["issuer_id"]:
                    return ActorContext(
                        actor_type=ActorType.USER,
                        actor_id=row["issuer_id"],
                        is_authenticated=True,
                        auth_method=AuthMethod.API_KEY,
                        tenant_id=None,
                        role=row.get("role_name"),
                    )
    except Exception as exc:
        logger.warning("api_key lookup failed: %s", exc)

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,
        is_authenticated=False,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        role=None,
    )


def _extract_session_actor(session_token: str) -> ActorContext:
    """세션 쿠키로 user actor를 반환한다.

    TODO: session resolver (Redis/DB) 연결 예정 (Phase 8).
    """
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,
        is_authenticated=False,
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role=None,
    )


def _extract_dev_header_actor(actor_id: str, actor_role: str) -> ActorContext:
    """개발용 헤더(X-Actor-Id + X-Actor-Role)로 인증된 actor를 반환한다.

    Phase 8 JWT 연동 전까지 테스트 및 개발 편의를 위해 사용.
    프로덕션에서는 settings.debug=False 시 이 경로를 차단해야 한다.
    """
    role = actor_role if actor_role in _VALID_ROLES else "VIEWER"
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=True,
        auth_method=AuthMethod.BEARER,
        tenant_id=None,
        role=role,
    )


def _anonymous_actor() -> ActorContext:
    return ActorContext(
        actor_type=ActorType.ANONYMOUS,
        actor_id=None,
        is_authenticated=False,
        auth_method=None,
        tenant_id=None,
        role=None,
    )
