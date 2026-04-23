"""
S3 Phase 0 / FG 0-5 — 문서별 재벡터화 쿨다운 (Valkey 우선, in-memory 폴백).

작업지시서 §4.3:
  - 문서당 쿨다운 10초 (동일 actor 기준)
  - Valkey `SET vec_reindex:{document_id}:{actor_id} 1 EX 10 NX`
  - 실패 시 `429 + Retry-After: <남은 초>`

폐쇄망 호환 (S2 ⑦):
  - Valkey 접속 불가 시 in-memory dict 로 폴백 (동일 프로세스 내 유효)
  - 경고 로그 1회 출력 후 계속 동작
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# In-memory fallback — 프로세스 로컬
# --------------------------------------------------------------------------- #


_inmem_lock = threading.Lock()
_inmem_expires: dict[str, float] = {}   # key -> epoch expiry


def _inmem_try_acquire(key: str, ttl_sec: int) -> tuple[bool, int]:
    """in-memory 쿨다운 — (acquired, remaining_seconds). acquired=True 면 lock 획득."""
    now = time.time()
    with _inmem_lock:
        exp = _inmem_expires.get(key)
        if exp is not None and exp > now:
            return False, int(round(exp - now))
        _inmem_expires[key] = now + ttl_sec
        # 주기적으로 만료 항목 청소 (많이 누적 방지)
        if len(_inmem_expires) > 1000:
            cutoff = now
            for k in [kk for kk, vv in _inmem_expires.items() if vv <= cutoff]:
                _inmem_expires.pop(k, None)
        return True, 0


def _inmem_remaining(key: str) -> int:
    with _inmem_lock:
        exp = _inmem_expires.get(key)
    if exp is None:
        return 0
    rem = int(round(exp - time.time()))
    return max(0, rem)


# --------------------------------------------------------------------------- #
# Valkey 우선 시도
# --------------------------------------------------------------------------- #


_VALKEY_WARN_EMITTED = False


def _valkey_try_acquire(key: str, ttl_sec: int) -> tuple[bool, int, bool]:
    """Valkey 기반 쿨다운 시도.

    반환: (acquired, remaining_seconds, valkey_ok)
    """
    global _VALKEY_WARN_EMITTED
    try:
        from app.cache import get_valkey  # noqa: WPS433
        r = get_valkey()
        # SET key 1 NX EX ttl
        ok = r.set(name=key, value="1", nx=True, ex=ttl_sec)
        if ok:
            return True, 0, True
        ttl = r.ttl(key)
        # -2 = key 없음 (이 경우 race 가능성 — 재시도 대신 보수적으로 락 없음으로 취급)
        if ttl is None or ttl < 0:
            return False, 1, True
        return False, int(ttl), True
    except Exception as exc:
        if not _VALKEY_WARN_EMITTED:
            logger.warning(
                "[FG0-5] Valkey 쿨다운 접속 실패 — in-memory 폴백 사용: %s",
                exc,
            )
            _VALKEY_WARN_EMITTED = True
        return False, 0, False


# --------------------------------------------------------------------------- #
# 공용 API
# --------------------------------------------------------------------------- #


_KEY_PREFIX = "vec_reindex"
DEFAULT_TTL_SEC = 10


@dataclass
class CooldownResult:
    acquired: bool
    remaining_sec: int
    backend: str   # "valkey" | "inmem"


def _key(document_id: str, actor_id: Optional[str]) -> str:
    return f"{_KEY_PREFIX}:{document_id}:{actor_id or 'anonymous'}"


def try_acquire(
    document_id: str,
    actor_id: Optional[str],
    *,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> CooldownResult:
    """쿨다운 획득 시도.

    반환:
      acquired=True  — 쿨다운 획득 (요청 처리 진행 가능)
      acquired=False — 이미 진행 중 (remaining_sec 초 대기 필요)
    """
    key = _key(document_id, actor_id)

    acquired, remaining, valkey_ok = _valkey_try_acquire(key, ttl_sec)
    if valkey_ok:
        return CooldownResult(
            acquired=acquired,
            remaining_sec=remaining if not acquired else 0,
            backend="valkey",
        )

    # Valkey 불가 → in-memory 폴백
    ok, rem = _inmem_try_acquire(key, ttl_sec)
    return CooldownResult(acquired=ok, remaining_sec=rem, backend="inmem")


def peek_remaining(document_id: str, actor_id: Optional[str]) -> int:
    """현재 남은 쿨다운 초. Valkey → in-memory 순서로 조회. 없으면 0."""
    key = _key(document_id, actor_id)
    try:
        from app.cache import get_valkey
        r = get_valkey()
        ttl = r.ttl(key)
        if ttl is not None and ttl > 0:
            return int(ttl)
    except Exception:
        pass
    return _inmem_remaining(key)
