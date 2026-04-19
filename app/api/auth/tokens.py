"""
JWT 토큰 발급 및 검증 유틸리티.

Phase 14 확장:
  - Access Token에 jti, type claim 추가
  - Refresh Token 생성 (opaque token, SHA-256 해시 저장)
  - 기존 create_access_token() 시그니처 하위 호환 유지

발급:
  create_access_token(actor_id, role, expires_minutes) → JWT 문자열
  create_refresh_token() → (raw_token, token_hash)

검증:
  decode_access_token(token) → payload dict | None
  verify_refresh_token_hash(raw, stored_hash) → bool

알고리즘: HS256
필수 클레임: sub (actor_id), role, exp, type, jti
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

import jwt

from app import config

_ALGORITHM = "HS256"


def create_access_token(
    actor_id: str,
    role: str,
    expires_minutes: Optional[int] = None,
) -> str:
    """HS256 서명된 JWT access token을 발급한다.

    Args:
        actor_id: 사용자 식별자 (sub 클레임).
        role: 역할명 (VIEWER / AUTHOR / ... / SUPER_ADMIN).
        expires_minutes: 만료 시간(분). None이면 settings.jwt_expire_minutes 사용.

    Returns:
        서명된 JWT 문자열.

    Raises:
        ValueError: jwt_secret이 설정되지 않은 경우.
    """
    if not config.settings.jwt_secret:
        raise ValueError("JWT_SECRET is not configured")

    expire_delta = timedelta(
        minutes=expires_minutes or config.settings.jwt_expire_minutes
    )
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": actor_id,
        "role": role,
        "iat": now,
        "exp": now + expire_delta,
        "jti": str(uuid4()),
        "type": "access",
    }
    return jwt.encode(payload, config.settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """JWT token을 검증하고 payload를 반환한다.

    서명 불일치, 만료, 형식 오류, type 불일치 시 None 반환.

    Returns:
        payload dict (sub, role, exp, jti, type, ...) 또는 None (검증 실패).
    """
    if not config.settings.jwt_secret:
        return None
    try:
        payload = jwt.decode(
            token,
            config.settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
        # Phase 14: type claim 검증 (하위 호환: type 없으면 허용)
        token_type = payload.get("type")
        if token_type is not None and token_type != "access":
            return None
        return payload
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# Phase 14: Refresh Token
# ---------------------------------------------------------------------------

def create_refresh_token() -> tuple[str, str]:
    """Opaque Refresh Token을 생성한다.

    DB에는 평문이 아닌 SHA-256 해시만 저장한다.
    클라이언트에는 raw_token을 HttpOnly Cookie로 전달한다.

    Returns:
        (raw_token, token_hash) 튜플.
        raw_token: 클라이언트에 전달할 64바이트 URL-safe 토큰.
        token_hash: DB에 저장할 SHA-256 해시 (hex digest, 64자).
    """
    raw_token = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return raw_token, token_hash


def verify_refresh_token_hash(raw_token: str, stored_hash: str) -> bool:
    """클라이언트에서 받은 raw_token의 해시가 DB 저장값과 일치하는지 확인한다.

    타이밍 공격 방어를 위해 hmac.compare_digest를 사용한다.

    Args:
        raw_token: 클라이언트로부터 받은 opaque 토큰.
        stored_hash: DB에 저장된 SHA-256 해시.

    Returns:
        일치 여부.
    """
    computed = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, stored_hash)


def generate_family_id() -> str:
    """새로운 RT family ID를 생성한다.

    Returns:
        UUID4 문자열.
    """
    return str(uuid4())


# ---------------------------------------------------------------------------
# JWT 액세스 토큰 블랙리스트 (로그아웃 취소 지원)
# ---------------------------------------------------------------------------

_AT_BLACKLIST_PREFIX = "at_blacklist"


def blacklist_access_token(valkey, jti: str, ttl_seconds: int) -> None:
    """로그아웃된 액세스 토큰의 jti를 Valkey 블랙리스트에 등록한다.

    TTL은 토큰 잔여 유효 시간으로 설정하여 만료 후 자동 제거.

    Args:
        valkey: Valkey 클라이언트.
        jti: 토큰의 jti 클레임.
        ttl_seconds: 블랙리스트 엔트리 유지 시간(초).
    """
    if not jti or ttl_seconds <= 0:
        return
    key = f"{_AT_BLACKLIST_PREFIX}:{jti}"
    valkey.setex(key, ttl_seconds, "1")


def is_access_token_blacklisted(valkey, jti: str) -> bool:
    """액세스 토큰의 jti가 블랙리스트에 있는지 확인한다.

    Args:
        valkey: Valkey 클라이언트.
        jti: 토큰의 jti 클레임.

    Returns:
        블랙리스트에 있으면 True.
    """
    if not jti:
        return False
    key = f"{_AT_BLACKLIST_PREFIX}:{jti}"
    return valkey.exists(key) > 0
