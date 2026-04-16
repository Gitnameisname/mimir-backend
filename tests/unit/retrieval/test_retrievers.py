"""
Retriever 플러그인 단위 테스트 — Task 2-4
DB 없이 Mock 기반으로 실행.
"""
from __future__ import annotations

from uuid import uuid4
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.schemas.citation import Citation
from app.services.retrieval.base import RetrievalResult
from app.services.retrieval.hybrid_retriever import HybridRetriever, _RRF_K
from app.services.retrieval.retriever_factory import RetrieverFactory
from app.services.retrieval.fts_retriever import FTSRetriever
from app.services.retrieval.vector_retriever import VectorRetriever


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def _make_result(
    score: float = 0.9,
    doc_id=None,
    node_id=None,
) -> RetrievalResult:
    doc = doc_id or uuid4()
    node = node_id or uuid4()
    return RetrievalResult(
        document_id=doc,
        version_id=uuid4(),
        node_id=node,
        content="test content",
        score=score,
        citation=Citation.from_chunk(doc, uuid4(), node, "test content"),
    )


# ── RetrieverFactory ─────────────────────────────────────────────────────────

def test_factory_creates_fts():
    conn = MagicMock()
    r = RetrieverFactory.create("fts", conn)
    assert isinstance(r, FTSRetriever)


def test_factory_creates_vector():
    conn = MagicMock()
    r = RetrieverFactory.create("vector", conn, {"similarity_threshold": 0.5})
    assert isinstance(r, VectorRetriever)
    assert r._threshold == 0.5


def test_factory_creates_hybrid():
    conn = MagicMock()
    r = RetrieverFactory.create("hybrid", conn, {"fts_weight": 0.3, "vector_weight": 0.7})
    assert isinstance(r, HybridRetriever)
    assert r._fts_weight == 0.3
    assert r._vector_weight == 0.7


def test_factory_unknown_raises():
    conn = MagicMock()
    with pytest.raises(ValueError, match="Unknown retriever"):
        RetrieverFactory.create("colbert", conn)


def test_factory_default_hybrid_weights():
    conn = MagicMock()
    r = RetrieverFactory.create("hybrid", conn)
    assert r._fts_weight == 0.4
    assert r._vector_weight == 0.6


# ── HybridRetriever RRF ──────────────────────────────────────────────────────

def test_rrf_merge_returns_top_k():
    fts = MagicMock()
    vec = MagicMock()
    hybrid = HybridRetriever(fts, vec)

    results = [_make_result(float(i)) for i in range(5)]
    merged = hybrid._rrf_merge(results, [], top_k=3)
    assert len(merged) == 3


def test_rrf_merge_boost_overlap():
    """동일 결과가 두 리스트에 있으면 점수가 합산되어야 한다."""
    fts = MagicMock()
    vec = MagicMock()
    hybrid = HybridRetriever(fts, vec)

    doc_id = uuid4()
    node_id = uuid4()
    r = _make_result(doc_id=doc_id, node_id=node_id)

    # 두 리스트 모두 1위에 동일 결과
    merged = hybrid._rrf_merge([r], [r], top_k=1)
    expected = (0.4 / (_RRF_K + 1)) + (0.6 / (_RRF_K + 1))
    assert abs(merged[0].score - expected) < 1e-9


def test_rrf_merge_empty_fts():
    """FTS 결과 없어도 Vector 결과로 응답해야 한다."""
    fts = MagicMock()
    vec = MagicMock()
    hybrid = HybridRetriever(fts, vec)

    vec_results = [_make_result(0.8)]
    merged = hybrid._rrf_merge([], vec_results, top_k=5)
    assert len(merged) == 1


def test_rrf_merge_preserves_citation():
    """병합된 결과에 citation이 유지되어야 한다."""
    fts = MagicMock()
    vec = MagicMock()
    hybrid = HybridRetriever(fts, vec)

    r = _make_result(0.9)
    merged = hybrid._rrf_merge([r], [], top_k=1)
    assert merged[0].citation is not None
    assert merged[0].citation.verify("test content") is True


@pytest.mark.asyncio
async def test_hybrid_retrieve_parallel():
    """FTS + Vector를 병렬 호출해야 한다."""
    fts_mock = MagicMock()
    vec_mock = MagicMock()

    fts_mock.retrieve = AsyncMock(return_value=[_make_result(0.9)])
    vec_mock.retrieve = AsyncMock(return_value=[_make_result(0.8)])
    # _warn_if_no_acl 패치
    fts_mock._warn_if_no_acl = MagicMock()
    vec_mock._warn_if_no_acl = MagicMock()

    hybrid = HybridRetriever(fts_mock, vec_mock)
    hybrid._warn_if_no_acl = MagicMock()

    results = await hybrid.retrieve(
        "test query", "POLICY", top_k=5,
        filters={"actor_role": "VIEWER"},
    )
    assert fts_mock.retrieve.called
    assert vec_mock.retrieve.called
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_hybrid_retrieve_fts_failure_returns_vec():
    """FTS 실패 시 Vector 결과만 반환해야 한다."""
    fts_mock = MagicMock()
    vec_mock = MagicMock()

    fts_mock.retrieve = AsyncMock(side_effect=Exception("FTS timeout"))
    vec_mock.retrieve = AsyncMock(return_value=[_make_result(0.8)])
    fts_mock._warn_if_no_acl = MagicMock()
    vec_mock._warn_if_no_acl = MagicMock()

    hybrid = HybridRetriever(fts_mock, vec_mock)
    hybrid._warn_if_no_acl = MagicMock()

    results = await hybrid.retrieve("query", "POLICY", 5, {"actor_role": "VIEWER"})
    assert len(results) >= 1


# ── ACL 경고 ─────────────────────────────────────────────────────────────────

def test_retriever_warns_without_acl(caplog):
    """actor_role 없이 호출 시 경고 로그가 출력되어야 한다."""
    import logging
    fts = FTSRetriever(MagicMock())
    with caplog.at_level(logging.WARNING):
        fts._warn_if_no_acl(None)
    assert "actor_role" in caplog.text
