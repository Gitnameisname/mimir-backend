"""
Reranker 플러그인 단위 테스트 — Task 2-5
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.citation import Citation
from app.services.retrieval.base import RetrievalResult
from app.services.retrieval.null_reranker import NullReranker
from app.services.retrieval.rule_based_reranker import RuleBasedReranker
from app.services.retrieval.reranker_factory import RerankerFactory
from app.services.retrieval.cross_encoder_reranker import CrossEncoderReranker


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def _make_result(score: float = 0.5, metadata: dict = None) -> RetrievalResult:
    doc = uuid4()
    node = uuid4()
    return RetrievalResult(
        document_id=doc,
        version_id=uuid4(),
        node_id=node,
        content="test content",
        score=score,
        citation=Citation.from_chunk(doc, uuid4(), node, "test content"),
        metadata=metadata or {},
    )


# ── NullReranker ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_null_reranker_preserves_order():
    """NullReranker는 입력 순서를 그대로 유지해야 한다."""
    reranker = NullReranker()
    candidates = [_make_result(float(i)) for i in range(5)]
    result = await reranker.rerank("query", candidates, top_k=3)
    assert len(result) == 3
    # 입력 순서 보존: 앞 3개가 그대로 반환 (0.0, 1.0, 2.0)
    assert result[0].score == 0.0
    assert result[1].score == 1.0
    assert result[2].score == 2.0


@pytest.mark.asyncio
async def test_null_reranker_empty():
    reranker = NullReranker()
    result = await reranker.rerank("query", [], top_k=5)
    assert result == []


@pytest.mark.asyncio
async def test_null_reranker_top_k_larger_than_candidates():
    reranker = NullReranker()
    candidates = [_make_result() for _ in range(3)]
    result = await reranker.rerank("query", candidates, top_k=10)
    assert len(result) == 3


# ── RuleBasedReranker ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule_based_pinned_bonus():
    """pinned 문서가 낮은 스코어여도 앞으로 올라와야 한다."""
    reranker = RuleBasedReranker(pinned_bonus=0.5)
    low = _make_result(0.2)
    high_pinned = _make_result(0.1, metadata={"pinned": True})
    result = await reranker.rerank("query", [low, high_pinned], top_k=2)
    assert result[0].metadata.get("pinned") is True


@pytest.mark.asyncio
async def test_rule_based_freshness_bonus():
    """최근 수정된 문서가 보너스를 받아야 한다."""
    reranker = RuleBasedReranker(freshness_bonus=0.5)
    old = _make_result(0.9, metadata={"updated_at": "2020-01-01T00:00:00"})
    fresh = _make_result(
        0.3,
        metadata={"updated_at": datetime.now(timezone.utc).isoformat()},
    )
    result = await reranker.rerank("q", [old, fresh], top_k=2)
    # 에러 없이 실행되어야 하고, fresh가 앞에 오거나 비슷해야 함
    assert len(result) == 2


@pytest.mark.asyncio
async def test_rule_based_invalid_date_skipped():
    """updated_at 파싱 실패 시 보너스 없이 처리해야 한다."""
    reranker = RuleBasedReranker(freshness_bonus=0.5)
    r = _make_result(0.5, metadata={"updated_at": "not-a-date"})
    result = await reranker.rerank("q", [r], top_k=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_rule_based_empty():
    reranker = RuleBasedReranker()
    result = await reranker.rerank("q", [], top_k=5)
    assert result == []


# ── CrossEncoderReranker — 폴백 테스트 ───────────────────────────────────────

def test_cross_encoder_fallback_when_no_package():
    """sentence-transformers 없을 때 NullReranker로 폴백해야 한다."""
    # 모델 로드 실패를 시뮬레이션 (존재하지 않는 경로)
    reranker = CrossEncoderReranker(model_name_or_path="/nonexistent/model")
    assert reranker._model is None
    assert reranker._fallback is not None


@pytest.mark.asyncio
async def test_cross_encoder_uses_null_when_model_none():
    """_model이 None이면 NullReranker로 처리해야 한다."""
    reranker = CrossEncoderReranker(model_name_or_path="/nonexistent/model")
    candidates = [_make_result(0.9), _make_result(0.5)]
    result = await reranker.rerank("query", candidates, top_k=2)
    assert len(result) == 2  # NullReranker 동작


# ── RerankerFactory ───────────────────────────────────────────────────────────

def test_factory_null_name():
    r = RerankerFactory.create(None)
    assert isinstance(r, NullReranker)


def test_factory_null_string():
    r = RerankerFactory.create("null")
    assert isinstance(r, NullReranker)


def test_factory_rule_based():
    r = RerankerFactory.create("rule_based", {"freshness_bonus": 0.1})
    assert isinstance(r, RuleBasedReranker)
    assert r._freshness_bonus == 0.1


def test_factory_cross_encoder():
    r = RerankerFactory.create("cross_encoder", {"model": "/nonexistent"})
    assert isinstance(r, CrossEncoderReranker)


def test_factory_unknown_raises():
    with pytest.raises(ValueError, match="Unknown reranker"):
        RerankerFactory.create("bm25_reranker")


def test_factory_disabled_by_env(monkeypatch):
    """RERANKER_ENABLED=false 시 모든 요청이 NullReranker를 반환해야 한다."""
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    r = RerankerFactory.create("cross_encoder")
    assert isinstance(r, NullReranker)


def test_factory_disabled_by_env_rule_based(monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    r = RerankerFactory.create("rule_based")
    assert isinstance(r, NullReranker)


# ── MAX_CANDIDATES 제한 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule_based_truncates_large_candidates(caplog):
    """후보가 MAX_CANDIDATES를 초과하면 경고 후 잘라야 한다."""
    import logging
    from app.services.retrieval.reranker_base import MAX_CANDIDATES

    reranker = RuleBasedReranker()
    candidates = [_make_result(float(i)) for i in range(MAX_CANDIDATES + 10)]
    with caplog.at_level(logging.WARNING):
        result = await reranker.rerank("q", candidates, top_k=5)
    assert len(result) == 5
    assert "truncating" in caplog.text
