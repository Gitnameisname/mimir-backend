"""
목적별 1회용 JWT 토큰 (Phase 14-5).

용도:
  - password_reset: 비밀번호 재설정 링크에 포함
  - email_verify: 이메일 인증 링크에 포함

보안 원칙:
  - 목적(purpose) claim으로 토큰 교차 사용 방지
  - jti claim + Valkey used_token:{jti} 키로 1회성 보장
  - 짧은 TTL (password_reset: 30분, email_verify: 24시간)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app.config import settings

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"

# Valkey 키 프리픽스 및 TTL (1회성 보장)
USED_TOKEN_PREFIX = "used_token"


def create_purpose_token(
    user_id: str,
    purpose: str,
    expire_minutes: int = 30,
) -> str:
    """목적별 1회용 JWT 토큰을 생성한다.

    Args:
        user_id: 대상 사용자 ID (sub 클레임).
        purpose: 토큰 목적 ("password_reset" | "email_verify").
        expire_minutes: 만료 시간(분). 기본 30분.

    Returns:
        서명된 JWT 문자열.

    Raises:
        ValueError: jwt_secret이 미설정된 경우.
    """
    if not settings.jwt_secret:
        raise ValueError("JWT_SECRET is not configured")

    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": user_id,
        "purpose": purpose,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_purpose_token(
    token: str,
    expected_purpose: str,
) -> dict | None:
    """목적별 토큰을 디코딩하고 목적을 검증한다.

    Args:
        token: JWT 문자열.
        expected_purpose: 기대하는 목적 값.

    Returns:
        payload dict (sub, purpose, jti, ...) 또는 None (검증 실패 시).
    """
    if not settings.jwt_secret:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "exp", "purpose", "jti"]},
        )
    except jwt.InvalidTokenError:
        logger.warning("purpose_token_decode: invalid or expired token")
        return None

    if payload.get("purpose") != expected_purpose:
        logger.warning(
            "purpose_token_decode: purpose mismatch expected=%s got=%s",
            expected_purpose,
            payload.get("purpose"),
        )
        return None

    return payload


def check_token_used(valkey, jti: str) -> bool:
    """토큰이 이미 사용되었는지 확인한다.

    Args:
        valkey: Valkey 클라이언트.
        jti: 토큰의 고유 식별자.

    Returns:
        이미 사용된 경우 True.
    """
    key = f"{USED_TOKEN_PREFIX}:{jti}"
    return valkey.exists(key) > 0


def mark_token_used(valkey, jti: str, ttl_seconds: int = 1800) -> None:
    """토큰을 사용 완료로 표시한다.

    Args:
        valkey: Valkey 클라이언트.
        jti: 토큰의 고유 식별자.
        ttl_seconds: Valkey 키 TTL (기본 30분 = 토큰 만료 시간).
    """
    key = f"{USED_TOKEN_PREFIX}:{jti}"
    valkey.setex(key, ttl_seconds, "1")
