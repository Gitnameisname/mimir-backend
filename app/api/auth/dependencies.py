"""
Actor extraction dependency — resolve_current_actor.

FastAPI dependency로 사용하며, 요청에서 actor를 추출해
ActorContext로 정규화하고 RequestContext.actor를 갱신한다.

인증 입력 소스 우선순위:
  1. X-Service-Token  → service actor (내부 서비스 간 호출) — HMAC-SHA256 검증
  2. Authorization: Bearer <JWT>  → user actor — HS256 서명 검증, sub/role 클레임 추출
  3. X-API-Key  → user actor — SHA-256 hash 검증 후 DB에서 issuer/role 조회
  4. Cookie: session=<token>  → user actor (TODO: Redis/DB 리졸버 Phase 9)
  5. 없음  → anonymous

개발용 헤더 (settings.debug=True 전용):
  - X-Actor-Id + X-Actor-Role : actor 직접 주입 (프로덕션에서 완전 비활성화)

보안 강화 내역:
  - VULN-001: X-Service-Token HMAC-SHA256 검증, secret 미설정 시 차단
  - VULN-002: 개발 헤더 게이트 settings.debug=True 전용
  - VULN-003: Bearer 경로의 X-Actor-Role 수락을 settings.debug 가드로 보호
  - VULN-004: API key — SHA-256 hash 검증 + hmac.compare_digest() 타이밍 공격 방지
  - VULN-005: Bearer JWT — HS256 서명 검증, exp/nbf 자동 확인, secret 미설정 시 차단
"""

import hashlib
import hmac
import logging

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.api.auth.models import ActorContext, ActorType, AuthMethod
from app.api.auth.tokens import is_access_token_blacklisted
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

    # 1. internal service header — HMAC 서명 검증 (VULN-001 수정)
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

    # 5. 개발용 헤더 (X-Actor-Id + X-Actor-Role) — debug=True + dev/test 환경 전용
    _DEV_ENVS = ("development", "test")
    if settings.debug and settings.environment in _DEV_ENVS:
        actor_id = request.headers.get("X-Actor-Id")
        actor_role = request.headers.get("X-Actor-Role")
        if actor_id and actor_role:
            return _extract_dev_header_actor(actor_id, actor_role)

    # 6. anonymous
    return _anonymous_actor()


def _extract_service_actor(token: str) -> ActorContext:
    """내부 서비스 토큰으로 service actor를 반환한다.

    VULN-001 수정: settings.internal_service_secret과 HMAC-SHA256 비교.
    - secret이 설정되지 않은 경우(빈 문자열): 해당 경로 차단 → anonymous 반환
    - 검증 실패: anonymous 반환 (fail-closed)
    - 검증 성공: is_authenticated=True SERVICE actor 반환

    TODO: Phase 8에서 JWT 방식으로 전환 예정.
    """
    secret = settings.internal_service_secret
    if not secret:
        logger.warning("X-Service-Token received but INTERNAL_SERVICE_SECRET not configured — rejecting")
        return ActorContext(
            actor_type=ActorType.ANONYMOUS,
            actor_id=None,
            is_authenticated=False,
            auth_method=None,
            tenant_id=None,
            role=None,
        )

    expected = hmac.HMAC(secret.encode(), b"mimir-internal", hashlib.sha256).hexdigest()
    if not hmac.compare_digest(token, expected):
        logger.warning("X-Service-Token HMAC verification failed")
        return ActorContext(
            actor_type=ActorType.ANONYMOUS,
            actor_id=None,
            is_authenticated=False,
            auth_method=None,
            tenant_id=None,
            role=None,
        )

    return ActorContext(
        actor_type=ActorType.SERVICE,
        actor_id=None,
        is_authenticated=True,
        auth_method=AuthMethod.INTERNAL_SERVICE,
        tenant_id=None,
        role=None,
    )


def _extract_bearer_actor(token: str, request: Request) -> ActorContext:
    """Bearer JWT로 user actor를 반환한다.

    검증 절차:
      1. settings.jwt_secret 미설정 시: debug 모드에서만 개발 헤더로 폴백, 그 외 anonymous
      2. jwt.decode()로 HS256 서명 검증 — exp/nbf 자동 확인
      3. payload의 'sub' → actor_id, 'role' → role 추출
      4. role이 없으면 DB에서 조회

    JWT 클레임 규약:
      - sub   : actor_id (사용자 UUID 또는 식별자)
      - role  : 역할명 (VIEWER/AUTHOR/REVIEWER/APPROVER/ORG_ADMIN/SUPER_ADMIN)
      - exp   : 만료 시각 (Unix timestamp)
    """
    secret = settings.jwt_secret

    if not secret:
        # jwt_secret 미설정 — debug 모드 + development/test 환경에서만 개발 헤더 폴백 허용
        _DEV_ENVS = ("development", "test")
        if settings.debug and settings.environment in _DEV_ENVS:
            logger.warning(
                "JWT_SECRET not configured — falling back to X-Actor-Id/X-Actor-Role headers "
                "(development only, never use in production)"
            )
            actor_id = request.headers.get("X-Actor-Id") or None
            actor_role = request.headers.get("X-Actor-Role")
            role: str | None = actor_role if actor_role in _VALID_ROLES else None
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
        logger.warning("Bearer token received but JWT_SECRET not configured — rejecting")
        return _anonymous_actor()

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        logger.info("Bearer token expired")
        return _anonymous_actor()
    except jwt.InvalidTokenError as exc:
        logger.warning("Bearer token invalid: %s", exc)
        return _anonymous_actor()

    actor_id: str | None = payload.get("sub")
    role_claim: str | None = payload.get("role")
    role = role_claim if role_claim in _VALID_ROLES else None
    jti: str = payload.get("jti", "")

    # SEC3-BE-002: 블랙리스트(로그아웃된 AT) 체크
    if jti:
        try:
            from app.cache.valkey import get_valkey
            valkey = get_valkey()
            if is_access_token_blacklisted(valkey, jti):
                logger.info("Bearer token revoked (blacklisted jti=%s)", jti)
                return _anonymous_actor()
        except Exception as exc:
            logger.warning("AT blacklist check failed (jti=%s): %s — allowing token", jti, exc)

    # role이 JWT에 없으면 DB에서 조회
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


def _hash_api_key(api_key: str) -> str:
    """API key를 SHA-256으로 해시한다."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _extract_api_key_actor(api_key: str) -> ActorContext:
    """API key로 actor를 반환한다.

    검증 절차:
      1. key_prefix(앞 8자리)로 후보 행 조회 — DB 인덱스 활용
      2. 전달된 key의 SHA-256 hash와 DB의 key_hash를 hmac.compare_digest()로 비교
      3. 불일치 시 인증 실패 (fail-closed)
      4. principal_type='agent'이면 AGENT ActorContext + kill-switch 확인
    """
    _FAIL = ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,
        is_authenticated=False,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        role=None,
    )
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                prefix = api_key[:8] if len(api_key) >= 8 else api_key
                cur.execute(
                    """
                    SELECT ak.id AS api_key_id, ak.issuer_id, ak.key_hash, ak.principal_type,
                           ak.agent_id, ak.scope_profile_id, ak.expires_at,
                           u.role_name
                    FROM api_keys ak
                    LEFT JOIN users u ON u.id::text = ak.issuer_id
                    WHERE ak.key_prefix = %s AND ak.status = 'ACTIVE'
                      AND (ak.expires_at IS NULL OR ak.expires_at > NOW())
                    LIMIT 1
                    """,
                    (prefix,),
                )
                row = cur.fetchone()
                if not row:
                    return _FAIL

                # SHA-256 hash 비교 — 타이밍 공격 방지
                expected_hash = row["key_hash"]
                provided_hash = _hash_api_key(api_key)
                if not hmac.compare_digest(provided_hash, expected_hash):
                    logger.warning("api_key hash mismatch for prefix=%s", prefix)
                    return _FAIL

                # last_used_at 갱신 (실패 무시)
                try:
                    cur.execute(
                        "UPDATE api_keys SET last_used_at = NOW(), use_count = use_count + 1"
                        " WHERE key_prefix = %s",
                        (prefix,),
                    )
                except Exception as tracking_exc:
                    logger.warning(
                        "api_key last_used_at update failed (key_prefix=%s): %s",
                        prefix,
                        tracking_exc,
                    )

                principal_type = (row.get("principal_type") or "user").lower()

                # S2 Phase 4: agent principal 처리
                if principal_type == "agent" and row.get("agent_id"):
                    return _extract_agent_context(conn, row)

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

    return _FAIL


def _extract_agent_context(conn, api_key_row) -> ActorContext:
    """API Key 행에서 AGENT ActorContext를 생성한다.

    킬스위치(is_disabled=True) 확인 — 비활성 에이전트는 anonymous 반환.
    REC-4.3: 에이전트 키는 expires_at 필수 — NULL이면 거부.
    """
    agent_id = str(api_key_row["agent_id"])
    _FAIL = ActorContext(
        actor_type=ActorType.ANONYMOUS,
        actor_id=None,
        is_authenticated=False,
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,
        role=None,
    )

    if api_key_row.get("expires_at") is None:
        logger.warning(
            "agent api_key rejected: expires_at is NULL (agent_id=%s). "
            "Agent keys must have an explicit expiration.",
            agent_id,
        )
        return _FAIL

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_disabled, organization_id, scope_profile_id FROM agents WHERE id = %s",
                (agent_id,),
            )
            agent_row = cur.fetchone()
        if not agent_row:
            logger.warning("agent_id=%s not found in agents table", agent_id)
            return _FAIL
        if agent_row["is_disabled"]:
            logger.warning("agent_id=%s is kill-switched — rejecting", agent_id)
            return _FAIL

        scope_profile_id = (
            str(api_key_row.get("scope_profile_id"))
            if api_key_row.get("scope_profile_id")
            else (str(agent_row["scope_profile_id"]) if agent_row.get("scope_profile_id") else None)
        )

        return ActorContext(
            actor_type=ActorType.AGENT,
            actor_id=agent_id,
            is_authenticated=True,
            auth_method=AuthMethod.API_KEY,
            tenant_id=str(agent_row["organization_id"]) if agent_row.get("organization_id") else None,
            role=None,
            agent_id=agent_id,
            scope_profile_id=scope_profile_id,
        )
    except Exception as exc:
        logger.warning("agent context extraction failed agent_id=%s: %s", agent_id, exc)
        return _FAIL


def _extract_session_actor(session_token: str) -> ActorContext:
    """세션 쿠키로 user actor를 반환한다.

    Valkey에서 session:{token} 키를 조회해 actor_id/role을 추출한다.
    세션 없음 / 만료 / Valkey 오류 → anonymous 반환 (fail-closed).
    """
    from app.api.auth.session import resolve_session

    session = resolve_session(session_token)
    if not session:
        return _anonymous_actor()

    actor_id: str | None = session.get("actor_id")
    role_claim: str | None = session.get("role")
    role = role_claim if role_claim in _VALID_ROLES else None

    if actor_id and not role:
        role = _lookup_role_from_db(actor_id)

    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=bool(actor_id),
        auth_method=AuthMethod.SESSION,
        tenant_id=None,
        role=role,
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


def require_authenticated(
    actor: ActorContext = Depends(resolve_current_actor),
) -> ActorContext:
    """인증된 actor만 허용한다. 미인증 요청은 401 Unauthorized로 거부한다.

    Default deny 원칙 (OWASP A05): 명시적 인증 없으면 모든 접근을 차단한다.
    """
    if not actor.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return actor


def require_role(*allowed_roles: str):
    """지정된 역할만 허용한다. 권한 없으면 403 Forbidden."""
    def _check(actor: ActorContext = Depends(require_authenticated)) -> ActorContext:
        if actor.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return actor
    return _check


def _anonymous_actor() -> ActorContext:
    return ActorContext(
        actor_type=ActorType.ANONYMOUS,
        actor_id=None,
        is_authenticated=False,
        auth_method=None,
        tenant_id=None,
        role=None,
    )
