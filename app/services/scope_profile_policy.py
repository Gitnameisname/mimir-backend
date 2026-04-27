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
    - process-local TTL 캐시 (기본 30s). Profile settings 변경 후 즉시 반영은
      약간 지연 허용. 강제 invalidate 가 필요한 호출자는 ``invalidate_cache()`` 호출.

함수 도서관: ``docs/함수도서관/backend.md`` §1.7-fg32 (FG 3-2 신설).
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
    "DEFAULT_CACHE_TTL_SEC",
]


DEFAULT_CACHE_TTL_SEC: int = 30


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


def invalidate_cache(scope_profile_id: Optional[str] = None) -> None:
    """전체 또는 특정 profile 의 캐시 비움.

    None 이면 전체 비움. 관리자 UI 가 settings 변경 후 호출 권장.
    """
    with _lock:
        if scope_profile_id is None:
            _cache.clear()
        else:
            _cache.pop(scope_profile_id, None)


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
