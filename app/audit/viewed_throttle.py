"""document.viewed emit dedup / throttle.

S3 Phase 3 FG 3-1 (task3-1) — Contributors 패널의 viewers 섹션 정확도와
audit_events 테이블 폭주 사이의 절충.

S3 Phase 7 FG 7-2 — cluster-wide dedup 도입.
    - 1차 시도: Valkey ``SET NX EX`` (다중 워커 원자성)
    - fallback: in-process LRU (기존 — fail-open 정책)

정책 (산출물 ``FG3-1_audit이벤트_실측.md`` §3.3 + ``Phase 7 개발계획서`` §1.2):
    - per (actor_id, document_id) 5분 윈도우 내 중복 view 는 1건으로 합침.
    - 윈도우 길이는 ``AUDIT_VIEWED_DEDUP_WINDOW_SEC`` 환경변수 (기본 300).
    - **Cluster-wide (FG 7-2)**: Valkey 가용 시 ``SET NX EX`` 로 원자적 dedup.
      Valkey 장애 / disabled 시 워커별 LRU fallback (R-I2 fail-open).
    - 캐시 상한 ``AUDIT_VIEWED_DEDUP_MAX_ENTRIES`` (기본 5000) 도달 시 LRU eviction.

이 모듈은 emit 자체를 수행하지 않는다 — emit 호출자가 :func:`should_emit_view`
를 먼저 호출해 dedup 여부만 판단한다.

함수 도서관 등록: ``docs/함수도서관/backend.md`` §1.7-fg31 (S3 Phase 3 신설,
Phase 7 FG 7-2 갱신).
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Optional

from app.utils.time import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "should_emit_view",
    "reset_for_tests",
    "DEFAULT_DEDUP_WINDOW_SEC",
    "DEFAULT_MAX_ENTRIES",
]


DEFAULT_DEDUP_WINDOW_SEC: int = 300
DEFAULT_MAX_ENTRIES: int = 5000


def _window_seconds() -> int:
    raw = os.environ.get("AUDIT_VIEWED_DEDUP_WINDOW_SEC")
    if not raw:
        return DEFAULT_DEDUP_WINDOW_SEC
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DEDUP_WINDOW_SEC
    return max(0, value)


def _max_entries() -> int:
    raw = os.environ.get("AUDIT_VIEWED_DEDUP_MAX_ENTRIES")
    if not raw:
        return DEFAULT_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ENTRIES
    return max(1, value)


_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, str], float]" = OrderedDict()


def _try_valkey_setnx(actor_id: str, document_id: str, window_sec: int) -> Optional[bool]:
    """Valkey ``SET NX EX`` 시도.

    Returns:
        ``True``: SETNX 성공 → 신규 → emit 허용
        ``False``: SETNX 실패 (이미 존재) → skip
        ``None``: Valkey 사용 불가 / 장애 → 호출자가 LRU fallback 수행
    """
    try:
        # 지연 import — 모듈 임포트 시점에 cache 의존성 강제하지 않음.
        # NOTE: 서브모듈에서 직접 import — 테스트 monkeypatch 가
        # ``app.cache.valkey.get_valkey_or_none`` 을 정확히 가리키도록 한다.
        from app.cache.valkey import get_valkey_or_none
        from app.cache.namespace import make_key
    except Exception as exc:  # pragma: no cover
        logger.debug("viewed_throttle: cache module import failed: %s", exc)
        return None

    client = get_valkey_or_none()
    if client is None:
        return None

    try:
        key = make_key("viewed", actor_id, document_id)
    except ValueError as exc:
        logger.warning("viewed_throttle: invalid key parts: %s", exc)
        return None

    try:
        # SET NX EX — 원자적. NX 성공 시 True, 이미 있으면 None.
        # decode_responses=True 이므로 string return.
        result = client.set(key, str(utcnow().timestamp()), nx=True, ex=window_sec)
    except Exception as exc:
        # ConnectionError / TimeoutError / RedisError — fail-open
        logger.debug("viewed_throttle: Valkey SETNX failed (%s) — falling back to LRU", exc)
        return None

    if result is True:
        return True
    # redis-py 는 NX 실패 시 None 반환
    return False


def should_emit_view(actor_id: Optional[str], document_id: str) -> bool:
    """현재 (actor_id, document_id) view 를 emit 해야 하는지 판단한다.

    Args:
        actor_id: 인증된 actor 의 식별자. ``None`` (anonymous) 이면 항상 ``False``
            반환 — Contributors 패널의 viewer 카운트는 인증 사용자만 의미가 있음.
        document_id: 대상 문서 UUID 문자열.

    Returns:
        bool: 윈도우 밖이면 ``True`` (emit), 윈도우 안 중복이면 ``False`` (skip).

    Notes:
        - **Cluster-wide (FG 7-2)**: Valkey 가용 시 다중 워커에서 정확히 1건만 ``True``.
        - **Fallback (R-I2 fail-open)**: Valkey 장애 / disabled 시 워커별 LRU.
        - 호출 자체가 LRU 갱신을 일으킨다 (정상 emit 흐름과 분리되지 않는다).
        - thread-safe (단일 ``threading.Lock``).
    """
    if not actor_id:
        return False
    if not document_id:
        return False

    window = _window_seconds()
    if window == 0:
        # 윈도우 0 = dedup off. 항상 emit 허용.
        return True

    # 1차 — cluster-wide Valkey SETNX
    valkey_result = _try_valkey_setnx(actor_id, document_id, window)
    if valkey_result is not None:
        return valkey_result

    # 2차 — in-process LRU fallback (R-I2 fail-open)
    now_ts = utcnow().timestamp()
    key = (actor_id, document_id)

    with _lock:
        previous_ts = _cache.get(key)
        if previous_ts is not None and (now_ts - previous_ts) < window:
            # 같은 viewer 의 같은 문서, 윈도우 내 — skip.
            # LRU 갱신은 하지 않는다 (이미 존재 + 시간 보존).
            return False

        # 신규 entry 또는 윈도우 만료된 entry — 갱신.
        _cache[key] = now_ts
        _cache.move_to_end(key)

        # eviction
        max_entries = _max_entries()
        while len(_cache) > max_entries:
            _cache.popitem(last=False)

    return True


def reset_for_tests() -> None:
    """테스트 격리용 — 캐시 비우기. 운영 코드에서 호출 금지."""
    with _lock:
        _cache.clear()
