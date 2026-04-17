"""RAG 전용 캐시 레이어 — Task 3-6.

검색 결과, Citation 검증 결과, 토큰 계산 값을 캐싱하여
멀티턴 RAG의 중복 연산을 줄인다.

설계 원칙:
  - S2 원칙 ⑦: Valkey 없으면 자동으로 in-memory LRU fallback
  - 모든 캐시 miss/hit 에러는 non-blocking (None 반환)
  - 캐시 키에 actor_id 포함 → 사용자 간 오염 방지
  - Document 업데이트 시 해당 document_id 관련 캐시 무효화
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 환경 설정
# ---------------------------------------------------------------------------

_EXTERNAL_ENABLED = os.getenv("EXTERNAL_DEPENDENCIES_ENABLED", "true").lower() != "false"

# TTL 설정 (초)
_TTL_SEARCH = int(os.getenv("RAG_CACHE_SEARCH_TTL", "3600"))    # 검색 결과 1시간
_TTL_VERIFY = int(os.getenv("RAG_CACHE_VERIFY_TTL", "86400"))   # 검증 결과 24시간
_TTL_TOKENS = int(os.getenv("RAG_CACHE_TOKENS_TTL", "3600"))    # 토큰 계산 1시간

# in-memory LRU 최대 항목 수
_LRU_MAX_SIZE = int(os.getenv("RAG_CACHE_LRU_MAX", "2000"))


# ---------------------------------------------------------------------------
# In-Memory LRU Cache (S2 원칙 ⑦ fallback)
# ---------------------------------------------------------------------------

class _LRUCache:
    """간단한 인메모리 LRU 캐시 (TTL 지원)."""

    def __init__(self, max_size: int = _LRU_MAX_SIZE) -> None:
        self._store: Dict[str, tuple[Any, datetime]] = {}
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if datetime.now(timezone.utc) > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        if len(self._store) >= self._max_size:
            # LRU 근사: 만료된 항목 제거
            now = datetime.now(timezone.utc)
            expired = [k for k, (_, e) in self._store.items() if e < now]
            for k in expired[:max(1, len(expired))]:
                del self._store[k]
            # 여전히 가득 차면 임의의 오래된 항목 제거
            if len(self._store) >= self._max_size:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
        self._store[key] = (value, datetime.now(timezone.utc) + timedelta(seconds=ttl))

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            del self._store[k]
        return len(keys)

    def clear(self) -> None:
        self._store.clear()


_lru = _LRUCache()


# ---------------------------------------------------------------------------
# Valkey 우선, 실패 시 LRU fallback
# ---------------------------------------------------------------------------

def _get(key: str) -> Optional[Any]:
    if _EXTERNAL_ENABLED:
        try:
            from app.cache.response_cache import get_cached
            return get_cached(key)
        except Exception as exc:
            logger.debug("Valkey get failed, falling back to LRU: %s", exc)
    return _lru.get(key)


def _set(key: str, value: Any, ttl: int) -> None:
    if _EXTERNAL_ENABLED:
        try:
            from app.cache.response_cache import set_cached
            set_cached(key, value, ttl)
            return
        except Exception as exc:
            logger.debug("Valkey set failed, falling back to LRU: %s", exc)
    _lru.set(key, value, ttl)


def _del_prefix(prefix: str) -> int:
    count = 0
    if _EXTERNAL_ENABLED:
        try:
            from app.cache.valkey import get_valkey
            r = get_valkey()
            keys = list(r.scan_iter(f"{prefix}*"))
            if keys:
                count += r.delete(*keys)
        except Exception as exc:
            logger.debug("Valkey del_prefix failed, falling back to LRU: %s", exc)
    count += _lru.delete_prefix(prefix)
    return count


# ---------------------------------------------------------------------------
# 캐시 키 생성
# ---------------------------------------------------------------------------

def _hash_key(*parts: Any) -> str:
    raw = ":".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 검색 결과 캐시
# ---------------------------------------------------------------------------

def get_search_cache(
    query: str,
    actor_id: Optional[str],
    top_k: int,
    document_type: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """캐시된 검색 결과를 반환한다. 없으면 None."""
    key = f"rag:search:{_hash_key(query, actor_id or '', top_k, document_type or '')}"
    cached = _get(key)
    if cached is not None:
        logger.debug("RAGCache hit (search): key=%s", key)
    return cached


def set_search_cache(
    query: str,
    actor_id: Optional[str],
    top_k: int,
    results: List[Dict[str, Any]],
    document_type: Optional[str] = None,
) -> None:
    """검색 결과를 캐싱한다."""
    key = f"rag:search:{_hash_key(query, actor_id or '', top_k, document_type or '')}"
    _set(key, results, _TTL_SEARCH)
    logger.debug("RAGCache set (search): key=%s ttl=%ds", key, _TTL_SEARCH)


def invalidate_search_cache_for_document(document_id: str) -> int:
    """특정 문서 관련 검색 캐시 무효화 (문서 업데이트 시 호출)."""
    # 검색 결과 캐시는 document_id 기반 추적이 어려워 전체 검색 캐시 무효화
    return _del_prefix("rag:search:")


# ---------------------------------------------------------------------------
# Citation 검증 캐시
# ---------------------------------------------------------------------------

def get_citation_verify_cache(content_hash: str) -> Optional[bool]:
    """캐시된 citation 검증 결과 (True=유효, False=무효, None=캐시없음)."""
    key = f"rag:citverify:{content_hash}"
    return _get(key)


def set_citation_verify_cache(content_hash: str, is_valid: bool) -> None:
    """Citation 검증 결과를 캐싱한다."""
    key = f"rag:citverify:{content_hash}"
    _set(key, is_valid, _TTL_VERIFY)


# ---------------------------------------------------------------------------
# 토큰 계산 캐시 (Turn 단위)
# ---------------------------------------------------------------------------

def get_token_cache(turn_id: str) -> Optional[Dict[str, int]]:
    """캐시된 턴 토큰 계산 결과를 반환한다."""
    key = f"rag:tokens:{turn_id}"
    return _get(key)


def set_token_cache(turn_id: str, token_counts: Dict[str, int]) -> None:
    """턴 토큰 계산 결과를 캐싱한다."""
    key = f"rag:tokens:{turn_id}"
    _set(key, token_counts, _TTL_TOKENS)


# ---------------------------------------------------------------------------
# 캐시 전체 무효화 (관리용)
# ---------------------------------------------------------------------------

def clear_all_rag_cache() -> None:
    """RAG 관련 전체 캐시를 무효화한다."""
    _del_prefix("rag:")
    _lru.clear()
    logger.info("RAGCache: all cache cleared")
