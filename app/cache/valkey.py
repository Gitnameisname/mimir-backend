"""
Valkey (Redis-compatible) 클라이언트 모듈.

redis-py ConnectionPool 기반 싱글톤 클라이언트.
Valkey는 Redis 프로토콜 호환이므로 redis-py를 그대로 사용한다.

사용법:
    from app.cache import get_valkey

    r = get_valkey()
    r.set("key", "value", ex=300)
    r.get("key")

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
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def ping_valkey() -> bool:
    """Valkey 연결 상태를 확인한다. 연결 불가 시 False 반환."""
    try:
        return get_valkey().ping()
    except Exception as exc:
        logger.warning("Valkey ping failed: %s", exc)
        return False


# 모듈 임포트 시 연결 테스트 (경고만, 실패해도 앱 기동 계속)
def _init() -> None:
    try:
        if ping_valkey():
            logger.info("Valkey connected: %s:%s", settings.valkey_host_clean, settings.valkey_port)
        else:
            logger.warning("Valkey not reachable at %s:%s", settings.valkey_host_clean, settings.valkey_port)
    except Exception as exc:
        logger.warning("Valkey init error: %s", exc)


# 클라이언트 인스턴스 (직접 import 용)
valkey_client = get_valkey
