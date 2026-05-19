"""
Valkey (Redis-compatible) 클라이언트 모듈.

redis-py ConnectionPool 기반 싱글톤 클라이언트.
Valkey는 Redis 프로토콜 호환이므로 redis-py를 그대로 사용한다.

사용법:
    from app.cache import get_valkey

    r = get_valkey()
    r.set("key", "value", ex=300)
    r.get("key")

폐쇄망 / disabled 모드 (S3 Phase 7 FG 7-1):
    - ``VALKEY_DISABLED=1`` 또는 ``VALKEY_HOST=""`` 시 ``is_valkey_disabled()`` True.
    - 이때 ``get_valkey_or_none()`` 는 ``None`` 반환 — 호출자는 명시적 fallback 경로 사용.
    - 기존 ``get_valkey()`` 는 호환성 유지 (실패 시 connection error 발생 가능).

연결 실패 처리:
  - 앱 시작 시 연결 불가 → 경고 로그만 출력 (startup 실패 아님)
  - 런타임 연결 실패 → 호출 지점에서 예외 처리 필요
"""

import logging
from typing import Optional

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def is_valkey_disabled() -> bool:
    """다음 중 하나면 True (S3 Phase 7 FG 7-1):

    - ``VALKEY_DISABLED`` 환경변수가 ``"1"`` / ``"true"`` / ``"yes"`` 중 하나
    - ``VALKEY_HOST`` 가 비어있음 (폐쇄망 / 단일 워커 환경)
    """
    flag = (settings.valkey_disabled or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if not settings.valkey_host_clean:
        return True
    return False


def _build_client() -> redis.Redis:
    """ConnectionPool 기반 Redis 클라이언트를 생성한다."""
    pool = redis.ConnectionPool.from_url(
        settings.valkey_url,
        max_connections=20,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    return redis.Redis(connection_pool=pool)


def get_valkey() -> redis.Redis:
    """모듈 레벨 싱글톤 클라이언트를 반환한다.

    첫 호출 시 ConnectionPool을 생성한다.
    연결 자체는 명령 실행 시점에 lazy하게 수립된다.

    NOTE: disabled 모드여도 본 함수는 호환성을 위해 Redis 인스턴스를 반환한다.
    명령 실행 시 ConnectionError 가 발생할 수 있다. 호출자는 try/except 처리
    또는 ``get_valkey_or_none()`` 사용을 권장.
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def get_valkey_or_none() -> Optional[redis.Redis]:
    """disabled 모드 시 ``None`` 반환. 정상 시 싱글톤 인스턴스.

    S3 Phase 7 FG 7-1 — 호출자가 명시적 fallback 경로를 가지도록 강제.
    """
    if is_valkey_disabled():
        return None
    return get_valkey()


def ping_valkey() -> bool:
    """Valkey 연결 상태를 확인한다. 연결 불가 시 False 반환."""
    if is_valkey_disabled():
        return False
    try:
        return get_valkey().ping()
    except Exception as exc:
        logger.warning("Valkey ping failed: %s", exc)
        return False


# 모듈 임포트 시 연결 테스트 (경고만, 실패해도 앱 기동 계속)
def _init() -> None:
    try:
        if is_valkey_disabled():
            logger.info("Valkey disabled (closed-network mode) — using in-process fallbacks")
            return
        if ping_valkey():
            logger.info("Valkey connected: %s:%s", settings.valkey_host_clean, settings.valkey_port)
        else:
            logger.warning("Valkey not reachable at %s:%s", settings.valkey_host_clean, settings.valkey_port)
    except Exception as exc:
        logger.warning("Valkey init error: %s", exc)


# 클라이언트 인스턴스 (직접 import 용)
valkey_client = get_valkey
