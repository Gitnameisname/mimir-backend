"""
Citation 재활용 + 캐싱 단위 테스트 — Task 3-6.

테스트 범위:
  - CitationReuseService.extract_cited_document_ids()
  - CitationReuseService.apply_citation_bonus(): 보너스 적용 + 재정렬
  - RAGCache: search / citation_verify / token 캐시 CRUD
  - Valkey fallback to in-memory LRU (EXTERNAL_DEPENDENCIES_ENABLED=false)
  - Document 업데이트 시 캐시 무효화
  - 성능: apply_citation_bonus 벤치마크
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


# ---------------------------------------------------------------------------
# 테스트 헬퍼
# ---------------------------------------------------------------------------

def _make_turn(doc_ids: list[str], turn_number: int = 1) -> "Turn":
    from app.models.conversation import Turn
    return Turn(
        id=str(uuid4()),
        conversation_id=str(uuid4()),
        turn_number=turn_number,
        user_message="질문",
        assistant_response="답변",
        retrieval_metadata={
            "citations": [
                {"document_id": did, "index": i + 1, "snippet": "s", "content_hash": "h"}
                for i, did in enumerate(doc_ids)
            ]
        },
        created_at=datetime.now(timezone.utc),
    )


def _make_result(doc_id: str, score: float) -> dict:
    return {"document_id": doc_id, "score": score, "content": "결과"}


# ===========================================================================
# 1. CitationReuseService
# ===========================================================================

class TestCitationReuseService:
    def test_extract_cited_document_ids_from_single_turn(self):
        """단일 Turn에서 document_id 추출."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        doc_id = str(uuid4())
        turn = _make_turn([doc_id])
        ids = svc.extract_cited_document_ids([turn])
        assert doc_id in ids

    def test_extract_cited_document_ids_multiple_turns(self):
        """여러 Turn에서 누적 추출 (중복 제거)."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        doc_a = str(uuid4())
        doc_b = str(uuid4())
        turns = [_make_turn([doc_a], 1), _make_turn([doc_b, doc_a], 2)]
        ids = svc.extract_cited_document_ids(turns)
        assert doc_a in ids
        assert doc_b in ids
        assert len(ids) == 2  # 중복 제거

    def test_extract_empty_when_no_metadata(self):
        """retrieval_metadata 없는 Turn → 빈 집합."""
        from app.services.citation_reuse_service import CitationReuseService
        from app.models.conversation import Turn
        svc = CitationReuseService()
        turn = Turn(
            id=str(uuid4()), conversation_id=str(uuid4()), turn_number=1,
            user_message="Q", assistant_response="A",
            retrieval_metadata={},
            created_at=datetime.now(timezone.utc),
        )
        ids = svc.extract_cited_document_ids([turn])
        assert len(ids) == 0

    def test_apply_citation_bonus_increases_score(self):
        """이전 인용 문서 점수가 BONUS_MULTIPLIER 배 증가."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        doc_id = str(uuid4())
        turn = _make_turn([doc_id])
        results = [_make_result(doc_id, 0.4), _make_result(str(uuid4()), 0.9)]

        boosted = svc.apply_citation_bonus(results, [turn])

        boosted_result = next(r for r in boosted if r["document_id"] == doc_id)
        assert boosted_result["score"] == pytest.approx(0.4 * CitationReuseService.CITATION_BONUS_MULTIPLIER)
        assert boosted_result.get("citation_reused") is True

    def test_apply_citation_bonus_reranks_results(self):
        """보너스 적용 후 점수 기준 재정렬."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        doc_cited = str(uuid4())
        doc_other = str(uuid4())
        turn = _make_turn([doc_cited])
        # cited: 0.3 (낮음), other: 0.9 (높음) → 보너스 후 cited 승리
        results = [_make_result(doc_cited, 0.3), _make_result(doc_other, 0.5)]

        boosted = svc.apply_citation_bonus(results, [turn])
        # cited 점수 = 0.3 * 1.5 = 0.45 > other 0.5? 아니면 < 0.5?
        # 0.3 * 1.5 = 0.45 < 0.5 → other 가 여전히 앞
        assert boosted[0]["document_id"] == doc_other

        # cited score가 0.4이면 보너스 후 0.6 → 역전
        results2 = [_make_result(doc_cited, 0.4), _make_result(doc_other, 0.5)]
        boosted2 = svc.apply_citation_bonus(results2, [turn])
        assert boosted2[0]["document_id"] == doc_cited

    def test_apply_citation_bonus_empty_previous_turns(self):
        """이전 턴 없으면 원본 순서 그대로 반환."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        results = [_make_result(str(uuid4()), 0.9), _make_result(str(uuid4()), 0.5)]
        original_order = [r["document_id"] for r in results]
        boosted = svc.apply_citation_bonus(results, [])
        assert [r["document_id"] for r in boosted] == original_order

    def test_get_reused_count(self):
        """재활용된 결과 수 반환."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        doc_a = str(uuid4())
        doc_b = str(uuid4())
        doc_c = str(uuid4())
        turns = [_make_turn([doc_a, doc_b])]
        results = [
            _make_result(doc_a, 0.9),
            _make_result(doc_b, 0.8),
            _make_result(doc_c, 0.7),
        ]
        assert svc.get_reused_count(results, turns) == 2


# ===========================================================================
# 2. RAGCache — 검색 결과 캐시
# ===========================================================================

class TestRAGCacheSearch:
    def test_cache_miss_returns_none(self):
        """미존재 키 → None."""
        from app.cache.rag_cache import get_search_cache
        result = get_search_cache("없는쿼리xyz123", "actor-1", 10)
        assert result is None

    def test_cache_set_and_hit(self):
        """set 후 get → 동일 값 반환."""
        from app.cache.rag_cache import get_search_cache, set_search_cache
        query = f"테스트쿼리_{uuid4().hex[:8]}"
        actor_id = str(uuid4())
        data = [{"document_id": str(uuid4()), "score": 0.9}]
        set_search_cache(query, actor_id, 5, data)
        cached = get_search_cache(query, actor_id, 5)
        assert cached is not None
        assert len(cached) == 1

    def test_different_actor_id_isolated(self):
        """actor_id가 다르면 다른 캐시 키."""
        from app.cache.rag_cache import get_search_cache, set_search_cache
        query = f"공유쿼리_{uuid4().hex[:8]}"
        data = [{"document_id": str(uuid4()), "score": 0.8}]
        set_search_cache(query, "actor-A", 5, data)
        # actor-B는 캐시 없어야 함
        result = get_search_cache(query, "actor-B", 5)
        assert result is None

    def test_invalidate_search_cache_for_document(self):
        """Document 업데이트 시 검색 캐시 무효화."""
        from app.cache.rag_cache import (
            get_search_cache, set_search_cache,
            invalidate_search_cache_for_document,
        )
        query = f"캐시무효화_{uuid4().hex[:8]}"
        actor_id = str(uuid4())
        data = [{"document_id": "doc-xyz", "score": 0.7}]
        set_search_cache(query, actor_id, 5, data)
        # 무효화
        invalidate_search_cache_for_document("doc-xyz")
        # 캐시 miss (전체 검색 캐시가 무효화됨)
        result = get_search_cache(query, actor_id, 5)
        assert result is None


# ===========================================================================
# 3. RAGCache — Citation 검증 캐시
# ===========================================================================

class TestRAGCacheCitationVerify:
    def test_citation_verify_cache_miss(self):
        from app.cache.rag_cache import get_citation_verify_cache
        assert get_citation_verify_cache("nonexistent_hash_xyz") is None

    def test_citation_verify_cache_set_valid(self):
        from app.cache.rag_cache import get_citation_verify_cache, set_citation_verify_cache
        h = f"hash_{uuid4().hex}"
        set_citation_verify_cache(h, True)
        assert get_citation_verify_cache(h) is True

    def test_citation_verify_cache_set_invalid(self):
        from app.cache.rag_cache import get_citation_verify_cache, set_citation_verify_cache
        h = f"hash_{uuid4().hex}"
        set_citation_verify_cache(h, False)
        assert get_citation_verify_cache(h) is False


# ===========================================================================
# 4. RAGCache — 토큰 계산 캐시
# ===========================================================================

class TestRAGCacheTokens:
    def test_token_cache_miss(self):
        from app.cache.rag_cache import get_token_cache
        assert get_token_cache("nonexistent_turn_id") is None

    def test_token_cache_set_and_hit(self):
        from app.cache.rag_cache import get_token_cache, set_token_cache
        turn_id = str(uuid4())
        counts = {"system": 50, "context": 200, "query": 30, "search": 100, "total": 380}
        set_token_cache(turn_id, counts)
        cached = get_token_cache(turn_id)
        assert cached == counts


# ===========================================================================
# 5. In-Memory LRU Fallback (EXTERNAL_DEPENDENCIES_ENABLED=false)
# ===========================================================================

class TestInMemoryFallback:
    def test_fallback_to_memory_when_external_disabled(self, monkeypatch):
        """EXTERNAL_DEPENDENCIES_ENABLED=false → in-memory LRU 사용."""
        monkeypatch.setenv("EXTERNAL_DEPENDENCIES_ENABLED", "false")
        import importlib
        import app.cache.rag_cache as mod
        importlib.reload(mod)

        query = f"폐쇄망쿼리_{uuid4().hex[:8]}"
        actor_id = str(uuid4())
        data = [{"document_id": str(uuid4()), "score": 0.85}]
        mod.set_search_cache(query, actor_id, 5, data)
        cached = mod.get_search_cache(query, actor_id, 5)
        assert cached is not None

        monkeypatch.delenv("EXTERNAL_DEPENDENCIES_ENABLED", raising=False)
        importlib.reload(mod)


# ===========================================================================
# 6. 성능 벤치마크 (apply_citation_bonus)
# ===========================================================================

class TestPerformanceBenchmark:
    def test_apply_bonus_performance_with_large_results(self):
        """500개 결과 × 20개 이전 Turn — 500ms 이내."""
        from app.services.citation_reuse_service import CitationReuseService
        svc = CitationReuseService()
        # 20개 이전 Turn, 각 3개 인용
        turns = [_make_turn([str(uuid4()) for _ in range(3)], i + 1) for i in range(20)]
        # 500개 검색 결과
        results = [_make_result(str(uuid4()), float(i) / 500) for i in range(500)]

        start = time.monotonic()
        boosted = svc.apply_citation_bonus(results, turns)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 500, f"apply_citation_bonus took {elapsed_ms:.1f}ms (limit 500ms)"
        assert len(boosted) == 500
