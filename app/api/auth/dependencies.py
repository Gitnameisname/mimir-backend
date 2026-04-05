"""
Actor extraction dependency — resolve_current_actor.

FastAPI dependency로 사용하며, 요청에서 actor를 추출해
ActorContext로 정규화하고 RequestContext.actor를 갱신한다.

책임 범위:
  - 인증 입력 소스(헤더/쿠키)를 확인해 actor_type 결정
  - anonymous / user / service 중 하나로 정규화
  - RequestContext.actor에 반영
  - ActorContext 반환 (downstream에서 사용)

이 dependency는 "누가 요청했는가"만 정규화한다.
"이 actor가 이 action을 할 수 있는가"는 AuthorizationService가 판단한다.

인증 입력 소스 우선순위:
  1. X-Service-Token  → service actor (내부 서비스 간 호출)
  2. Authorization: Bearer <token>  → user actor (JWT 등)
  3. X-API-Key  → user actor (API key)
  4. Cookie: session=<token>  → user actor (세션)
  5. 없음  → anonymous

TODO:
  - X-Service-Token 실제 검증 연결 예정 (internal_service auth)
  - Bearer token JWT verifier 연결 예정
  - API key store 조회 연결 예정
  - session resolver 연결 예정
  - actor_id, tenant_id 실제 채우기 예정
  - is_authenticated: 검증 성공 후 True로 전환 예정
"""

from fastapi import Request

from app.api.auth.models import ActorContext, ActorType, AuthMethod


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
            return _extract_bearer_actor(token)

    # 3. api key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return _extract_api_key_actor(api_key)

    # 4. session cookie
    session_token = request.cookies.get("session")
    if session_token:
        return _extract_session_actor(session_token)

    # 5. anonymous
    return _anonymous_actor()


def _extract_service_actor(token: str) -> ActorContext:
    """내부 서비스 토큰으로 service actor를 반환한다.

    TODO: X-Service-Token 실제 검증 로직 연결 예정.
          현재는 stub: 헤더 존재 여부만 확인하며 is_authenticated=False.
    """
    return ActorContext(
        actor_type=ActorType.SERVICE,
        actor_id=None,  # TODO: service identity 토큰에서 추출 예정
        is_authenticated=False,  # TODO: 검증 후 True
        auth_method=AuthMethod.INTERNAL_SERVICE,
        tenant_id=None,
    )


def _extract_bearer_actor(token: str) -> ActorContext:
    """Bearer 토큰으로 user actor를 반환한다.

    TODO: JWT verifier 연결 예정 (settings.jwt_secret 사용).
          현재는 stub: 헤더 존재 여부만 확인하며 is_authenticated=False.
    """
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,  # TODO: JWT claims sub 필드에서 추출 예정
        is_authenticated=False,  # TODO: 서명 검증 후 True
        auth_method=AuthMethod.BEARER,
        tenant_id=None,  # TODO: JWT claims tenant_id 추출 예정
    )


def _extract_api_key_actor(api_key: str) -> ActorContext:
    """API key로 user actor를 반환한다.

    TODO: API key store 조회 연결 예정.
          현재는 stub: 헤더 존재 여부만 확인하며 is_authenticated=False.
    """
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,  # TODO: API key → user_id 매핑 예정
        is_authenticated=False,  # TODO: key 검증 후 True
        auth_method=AuthMethod.API_KEY,
        tenant_id=None,  # TODO: API key scope에서 tenant_id 추출 예정
    )


def _extract_session_actor(session_token: str) -> ActorContext:
    """세션 쿠키로 user actor를 반환한다.

    TODO: session resolver (Redis/DB) 연결 예정.
          현재는 stub: 쿠키 존재 여부만 확인하며 is_authenticated=False.
    """
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=None,  # TODO: 세션 조회 후 user_id 채우기 예정
        is_authenticated=False,  # TODO: 세션 유효성 검증 후 True
        auth_method=AuthMethod.SESSION,
        tenant_id=None,  # TODO: 세션에서 tenant_id 추출 예정
    )


def _anonymous_actor() -> ActorContext:
    return ActorContext(
        actor_type=ActorType.ANONYMOUS,
        actor_id=None,
        is_authenticated=False,
        auth_method=None,
        tenant_id=None,
    )
