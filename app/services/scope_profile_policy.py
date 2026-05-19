"""Scope Profile 정책 게이트 — S3 Phase 3 FG 3-2.

목적:
    Scope Profile 의 ``settings_json`` 에 정의된 운영 정책을 호출자가 일관되게
    질의하기 위한 단일 진입점. 본 FG 의 첫 정책은 ``expose_viewers``.

원칙 (fail-closed):
    - viewer 의 scope_profile 조회 실패 / settings_json 파싱 실패 시
      **False (보수적)** 반환. 호출자는 viewers 노출을 차단해야 함.
    - admin 권한이 게이트를 우회하지 않는다 (정책은 actor-scope 기반).
    - 본 모듈은 scope 어휘를 하드코딩하지 않는다 (S2 ⑤). DB 조회 결과만 사용.

캐시:
    - process-local TTL 캐시 (기본 30s).
    - **S3 Phase 7 FG 7-3 — cluster-wide invalidation**: admin PATCH 가
      ``invalidate_cache(profile_id, broadcast=True)`` 호출 시 Valkey pub/sub
      브로드캐스트 발행. 모든 워커가 subscribe 되어 있어 즉시 process-local
      cache 비움.
    - Valkey 장애 / disabled 시: broadcast 는 best-effort skip. process-local
      TTL 30s 자연 만료에 의존 (다른 워커는 최대 30s stale 허용).

함수 도서관: ``docs/함수도서관/backend.md`` §1.7-fg32 (FG 3-2 신설, FG 7-3 갱신).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from app.api.auth.models import ActorContext
from app.db import get_db
from app.repositories.scope_profile_repository import ScopeProfileRepository
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


__all__ = [
    "should_expose_viewers",
    "invalidate_cache",
    "start_subscriber",
    "stop_subscriber",
    "DEFAULT_CACHE_TTL_SEC",
    "FEATURE_NAME",
]


DEFAULT_CACHE_TTL_SEC: int = 30
FEATURE_NAME: str = "scope_policy"


def _cache_ttl() -> int:
    raw = os.environ.get("SCOPE_PROFILE_POLICY_CACHE_TTL_SEC")
    if not raw:
        return DEFAULT_CACHE_TTL_SEC
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CACHE_TTL_SEC
    return max(0, value)


_lock = threading.Lock()
# {scope_profile_id: (expose_viewers_bool, fetched_at_ts)}
_cache: dict[str, tuple[bool, float]] = {}


def invalidate_cache(
    scope_profile_id: Optional[str] = None,
    *,
    broadcast: bool = False,
) -> None:
    """전체 또는 특정 profile 의 캐시 비움.

    Args:
        scope_profile_id: ``None`` 이면 전체 비움.
        broadcast: ``True`` 시 Valkey pub/sub 으로 다른 워커에도 invalidate
            브로드캐스트 (S3 Phase 7 FG 7-3). admin 정책 변경 호출자가 명시적
            으로 지정. subscriber 콜백은 loop 방지를 위해 ``broadcast=False``.

    Notes:
        - broadcast 는 best-effort. Valkey 장애 / disabled 시 silent skip.
        - 다른 워커의 process-local cache 는 broadcast 수신 시 즉시 비움.
          broadcast 실패 시 최대 TTL(30s) 자연 만료에 의존.
    """
    with _lock:
        if scope_profile_id is None:
            _cache.clear()
        else:
            _cache.pop(scope_profile_id, None)

    if not broadcast:
        return

    try:
        # 지연 import — pubsub 의존성을 모듈 임포트 시점에 강제하지 않음.
        from app.cache.pubsub import publish_invalidate
    except Exception as exc:  # pragma: no cover
        logger.debug("invalidate_cache: pubsub import failed: %s", exc)
        return

    key = scope_profile_id if scope_profile_id else "*"
    publish_invalidate(FEATURE_NAME, key)


def _handle_remote_invalidate(key: str) -> None:
    """다른 워커에서 받은 invalidate 메시지 처리.

    ``broadcast=False`` 로 호출 — pub/sub loop 방지.
    """
    if key == "*":
        invalidate_cache(None, broadcast=False)
    else:
        invalidate_cache(key, broadcast=False)


_subscriber = None


def start_subscriber() -> bool:
    """앱 startup 에서 호출. cluster-wide invalidation 수신용.

    Returns:
        ``True``: subscriber 시작 성공
        ``False``: Valkey disabled / 실패 → process-local 캐시만 사용
    """
    global _subscriber

    try:
        from app.cache.pubsub import Subscriber
    except Exception as exc:  # pragma: no cover
        logger.warning("start_subscriber: pubsub import failed: %s", exc)
        return False

    if _subscriber is not None:
        logger.debug("start_subscriber: already running")
        return True

    sub = Subscriber(FEATURE_NAME, on_invalidate=_handle_remote_invalidate)
    if not sub.start():
        return False

    _subscriber = sub
    logger.info("scope_profile_policy: cluster-wide invalidation subscriber started")
    return True


def stop_subscriber() -> None:
    """앱 shutdown 또는 테스트 격리용. daemon thread 이므로 운영에선 선택."""
    global _subscriber
    if _subscriber is not None:
        _subscriber.stop()
        _subscriber = None


def _get_expose_viewers_for_profile(scope_profile_id: str) -> bool:
    """캐시 우선 조회. miss 시 DB 조회. 실패 시 fail-closed False.

    psycopg2 connection 은 매 호출마다 잠깐 열고 닫음 (정책 조회는 가벼운 SELECT).
    """
    ttl = _cache_ttl()
    now_ts = utcnow().timestamp()

    if ttl > 0:
        with _lock:
            entry = _cache.get(scope_profile_id)
            if entry is not None:
                value, fetched_at = entry
                if (now_ts - fetched_at) < ttl:
                    return value

    try:
        with get_db() as conn:
            repo = ScopeProfileRepository(conn)
            profile = repo.get_by_id(scope_profile_id)
        if profile is None:
            value = False  # fail-closed
        else:
            value = bool(profile.settings.expose_viewers)
    except Exception as exc:  # pragma: no cover — DB 장애 등
        logger.warning(
            "scope_profile_policy: failed to load profile %s: %s",
            scope_profile_id,
            exc,
        )
        value = False  # fail-closed

    if ttl > 0:
        with _lock:
            _cache[scope_profile_id] = (value, now_ts)

    return value


def should_expose_viewers(viewer_actor: Optional[ActorContext]) -> bool:
    """viewer 의 ScopeProfile.settings.expose_viewers 를 fail-closed 로 반환.

    Args:
        viewer_actor: 현재 요청의 actor. None / anonymous / scope_profile_id 누락 모두
            보수적으로 ``False`` 반환.

    Returns:
        bool: True 면 viewers 노출 허용. False 면 차단.
    """
    if viewer_actor is None:
        return False
    if not getattr(viewer_actor, "is_authenticated", False):
        return False
    sp_id = getattr(viewer_actor, "scope_profile_id", None)
    if not sp_id:
        return False
    return _get_expose_viewers_for_profile(str(sp_id))
