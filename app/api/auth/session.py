"""
세션 관리 — Valkey 기반.

세션 토큰 → ActorContext 리졸버.
Valkey에 session:{token} 키로 JSON 직렬화된 세션 데이터를 저장한다.

세션 데이터 구조:
  {
    "actor_id": "...",
    "role": "AUTHOR",
    "created_at": 1712600000
  }

사용법:
  # 세션 생성 (로그인 핸들러에서 호출)
  from app.api.auth.session import create_session, delete_session
  token = create_session(actor_id="user-123", role="AUTHOR")

  # 세션 조회 (dependencies.py 내부)
  from app.api.auth.session import resolve_session
  data = resolve_session(token)  # dict | None
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from app.cache.valkey import get_valkey
from app.config import settings

logger = logging.getLogger(__name__)

_SESSION_PREFIX = "session:"
_SESSION_TTL_SECONDS = settings.jwt_expire_minutes * 60  # JWT와 동일한 수명 기본값


def _key(token: str) -> str:
    return f"{_SESSION_PREFIX}{token}"


def create_session(
    actor_id: str,
    role: str,
    ttl_seconds: Optional[int] = None,
) -> str:
    """새 세션을 Valkey에 저장하고 토큰을 반환한다.

    Args:
        actor_id: 사용자 식별자.
        role: 역할명.
        ttl_seconds: 세션 유효 시간(초). None이면 기본값(_SESSION_TTL_SECONDS) 사용.

    Returns:
        32바이트 URL-safe 랜덤 토큰 문자열.
    """
    token = secrets.token_urlsafe(32)
    payload = json.dumps({
        "actor_id": actor_id,
        "role": role,
        "created_at": int(datetime.now(tz=timezone.utc).timestamp()),
    })
    ttl = ttl_seconds or _SESSION_TTL_SECONDS
    try:
        get_valkey().set(_key(token), payload, ex=ttl)
    except Exception as exc:
        logger.error("Failed to create session for actor %s: %s", actor_id, exc)
        raise
    return token


def resolve_session(token: str) -> Optional[dict]:
    """세션 토큰으로 세션 데이터를 조회한다.

    Returns:
        {"actor_id": ..., "role": ..., "created_at": ...} 또는 None (없거나 만료).
    """
    try:
        raw = get_valkey().get(_key(token))
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Session resolve failed for token: %s", exc)
        return None


def delete_session(token: str) -> None:
    """세션을 즉시 파기한다 (로그아웃)."""
    try:
        get_valkey().delete(_key(token))
    except Exception as exc:
        logger.warning("Session delete failed: %s", exc)


def refresh_session(token: str, ttl_seconds: Optional[int] = None) -> bool:
    """세션 TTL을 연장한다 (슬라이딩 만료 지원).

    Returns:
        True: 연장 성공 / False: 세션 없음 또는 실패
    """
    try:
        ttl = ttl_seconds or _SESSION_TTL_SECONDS
        result = get_valkey().expire(_key(token), ttl)
        return bool(result)
    except Exception as exc:
        logger.warning("Session refresh failed: %s", exc)
        return False
