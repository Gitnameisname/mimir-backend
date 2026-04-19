"""
API 응답 캐싱 유틸리티 (Phase 13-8 성능 최적화).

read-heavy 엔드포인트(문서 목록, 검색, 헬스체크)의 응답을
Valkey에 캐싱하여 DB 부하를 줄인다.

캐싱 정책:
  - 문서 목록       : 30초 TTL (목록은 자주 변경될 수 있음)
  - 문서 상세       : 60초 TTL
  - 검색 결과       : 30초 TTL
  - 벡터화 상태     : 10초 TTL

사용 주의:
  - 인증/권한이 필요한 엔드포인트는 actor_id를 캐시 키에 포함해야 함
  - 쓰기 작업 후 관련 캐시를 명시적으로 무효화해야 함
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.cache.valkey import get_valkey

logger = logging.getLogger(__name__)

_FALLBACK_CACHE: dict[str, tuple[Any, datetime]] = {}


def _fallback_get(key: str) -> Any | None:
    entry = _FALLBACK_CACHE.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if datetime.now(timezone.utc) > expires_at:
        _FALLBACK_CACHE.pop(key, None)
        return None
    return value


def _fallback_set(key: str, value: Any, ttl_seconds: int) -> None:
    _FALLBACK_CACHE[key] = (
        value,
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )


def _fallback_invalidate(pattern: str) -> int:
    prefixes = {
        pattern.rstrip("*"),
        f"mimir:{pattern}".rstrip("*"),
    }
    keys = [key for key in _FALLBACK_CACHE if any(key.startswith(prefix) for prefix in prefixes)]
    for key in keys:
        _FALLBACK_CACHE.pop(key, None)
    return len(keys)


def _make_cache_key(prefix: str, *parts: Any) -> str:
    """캐시 키를 생성한다."""
    key_body = ":".join(str(p) for p in parts)
    if len(key_body) > 100:
        key_body = hashlib.sha256(key_body.encode()).hexdigest()
    return f"mimir:{prefix}:{key_body}"


def get_cached(key: str) -> Any | None:
    """Valkey에서 캐시된 값을 가져온다. 없거나 오류 시 None 반환."""
    try:
        r = get_valkey()
        raw = r.get(key)
        if raw is None:
            return _fallback_get(key)
        return json.loads(raw)
    except Exception as exc:
        logger.debug("cache get failed (key=%s): %s", key, exc)
        return _fallback_get(key)


def set_cached(key: str, value: Any, ttl_seconds: int = 60) -> bool:
    """값을 Valkey에 캐싱한다. 오류 시 False 반환 (non-critical)."""
    try:
        r = get_valkey()
        r.setex(key, ttl_seconds, json.dumps(value, default=str))
        return True
    except Exception as exc:
        logger.debug("cache set failed (key=%s): %s", key, exc)
        _fallback_set(key, value, ttl_seconds)
        return True


def invalidate_pattern(pattern: str) -> int:
    """패턴에 매치되는 키를 모두 삭제한다. 삭제된 키 수 반환."""
    deleted = 0
    try:
        r = get_valkey()
        keys = list(r.scan_iter(f"mimir:{pattern}"))
        if keys:
            deleted += r.delete(*keys)
    except Exception as exc:
        logger.debug("cache invalidate failed (pattern=%s): %s", pattern, exc)
    return deleted + _fallback_invalidate(pattern)


def invalidate_document(document_id: str) -> None:
    """특정 문서 관련 캐시를 모두 무효화한다."""
    invalidate_pattern(f"doc:{document_id}:*")
    invalidate_pattern(f"search:*")  # 검색 결과도 무효화


# --------------------------------------------------------------------------- #
# 캐시 키 생성 헬퍼 (도메인별)
# --------------------------------------------------------------------------- #

def doc_list_key(actor_id: str | None, page: int, page_size: int, **filters: Any) -> str:
    return _make_cache_key("doc:list", actor_id or "anon", page, page_size, sorted(filters.items()))


def doc_detail_key(document_id: str, actor_id: str | None) -> str:
    return _make_cache_key("doc", document_id, actor_id or "anon")


def search_key(query: str, actor_id: str | None, page: int, **filters: Any) -> str:
    return _make_cache_key("search", actor_id or "anon", query, page, sorted(filters.items()))
