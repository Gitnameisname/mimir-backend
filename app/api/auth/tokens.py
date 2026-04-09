"""
JWT 토큰 발급 및 검증 유틸리티.

발급:
  create_access_token(actor_id, role, expires_minutes) → JWT 문자열

검증:
  decode_access_token(token) → payload dict | None

알고리즘: HS256
필수 클레임: sub (actor_id), role, exp
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt

from app.config import settings

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
    if not settings.jwt_secret:
        raise ValueError("JWT_SECRET is not configured")

    expire_delta = timedelta(minutes=expires_minutes or settings.jwt_expire_minutes)
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": actor_id,
        "role": role,
        "iat": now,
        "exp": now + expire_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """JWT token을 검증하고 payload를 반환한다.

    서명 불일치, 만료, 형식 오류 시 None 반환 (예외 전파 없음).

    Returns:
        payload dict (sub, role, exp, ...) 또는 None (검증 실패).
    """
    if not settings.jwt_secret:
        return None
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.InvalidTokenError:
        return None
