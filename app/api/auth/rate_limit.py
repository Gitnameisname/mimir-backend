"""
로그인 시도 제한 모듈 (Phase 14).

Valkey 기반으로 이메일별 로그인 실패 횟수를 추적하고,
임계값 초과 시 일정 시간 동안 해당 이메일의 로그인을 차단한다.

키 패턴: login_attempts:{email}
TTL: 첫 실패 시 설정, 자동 만료로 잠금 해제 (별도 배치 불필요)

보안 원칙:
  - IP 기반이 아닌 이메일 기반 제한 (프록시/VPN 우회 방지)
  - TTL 기반 자동 해제로 메모리 누수 방지
"""

import logging
from typing import Optional

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_LOGIN_ATTEMPT_PREFIX = "login_attempts:"


def _key(email: str) -> str:
    """이메일을 기반으로 Valkey 키를 생성한다."""
    return f"{_LOGIN_ATTEMPT_PREFIX}{email.lower().strip()}"


def check_login_allowed(valkey: redis.Redis, email: str) -> bool:
    """로그인 가능 여부를 확인한다 (잠금 상태 검사).

    Args:
        valkey: Redis 클라이언트
        email: 확인할 이메일

    Returns:
        True이면 로그인 시도 허용, False이면 잠금 상태
    """
    try:
        count = valkey.get(_key(email))
        if count is None:
            return True
        return int(count) < settings.login_max_attempts
    except Exception as exc:
        # Valkey 연결 실패 시 로그인은 허용 (가용성 우선)
        logger.warning("rate_limit check failed (allowing login): %s", exc)
        return True


def record_failed_attempt(valkey: redis.Redis, email: str) -> int:
    """로그인 실패 횟수를 증가시킨다.

    첫 실패 시 TTL을 설정한다.

    Args:
        valkey: Redis 클라이언트
        email: 실패한 이메일

    Returns:
        증가 후 실패 횟수
    """
    key = _key(email)
    lockout_seconds = settings.login_lockout_minutes * 60

    try:
        count = valkey.incr(key)
        if count == 1:
            # 첫 실패: TTL 설정
            valkey.expire(key, lockout_seconds)
        return count
    except Exception as exc:
        logger.warning("rate_limit record failed: %s", exc)
        return 0


def clear_attempts(valkey: redis.Redis, email: str) -> None:
    """로그인 성공 시 실패 카운터를 초기화한다.

    Args:
        valkey: Redis 클라이언트
        email: 성공한 이메일
    """
    try:
        valkey.delete(_key(email))
    except Exception as exc:
        logger.warning("rate_limit clear failed: %s", exc)


def get_remaining_attempts(valkey: redis.Redis, email: str) -> Optional[int]:
    """남은 로그인 시도 횟수를 반환한다.

    Args:
        valkey: Redis 클라이언트
        email: 확인할 이메일

    Returns:
        남은 횟수 (None이면 제한 없음)
    """
    try:
        count = valkey.get(_key(email))
        if count is None:
            return settings.login_max_attempts
        remaining = settings.login_max_attempts - int(count)
        return max(0, remaining)
    except Exception:
        return None
