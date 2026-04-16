"""
Task 2-6: DocumentType retrieval_config + 검색 API 플러그인 파라미터 단위 테스트
"""
from __future__ import annotations

import json
import pytest

from app.schemas.retrieval_config import RetrievalConfig, RetrieverParams, RerankerParams


# ── RetrievalConfig 기본값 ───────────────────────────────────────────────────

def test_retrieval_config_defaults():
    """기본 RetrievalConfig는 fts retriever, no reranker이어야 한다."""
    config = RetrievalConfig()
    assert config.default_retriever == "fts"
    assert config.default_reranker is None
    assert isinstance(config.retriever_params, RetrieverParams)
    assert isinstance(config.reranker_params, RerankerParams)


def test_retriever_params_defaults():
    params = RetrieverParams()
    assert 0.0 <= params.fts_weight <= 1.0
    assert 0.0 <= params.vector_weight <= 1.0
    assert 0.0 <= params.similarity_threshold <= 1.0


def test_reranker_params_defaults():
    params = RerankerParams()
    assert params.model is None
    assert params.freshness_bonus >= 0.0
    assert params.pinned_bonus >= 0.0


# ── RetrievalConfig 파싱 ──────────────────────────────────────────────────────

def test_retrieval_config_parse_hybrid():
    raw = {
        "default_retriever": "hybrid",
        "retriever_params": {"fts_weight": 0.5, "vector_weight": 0.5, "similarity_threshold": 0.2},
        "default_reranker": "rule_based",
        "reranker_params": {"freshness_bonus": 0.1, "pinned_bonus": 0.2},
    }
    config = RetrievalConfig.model_validate(raw)
    assert config.default_retriever == "hybrid"
    assert config.default_reranker == "rule_based"
    assert config.retriever_params.fts_weight == 0.5
    assert config.reranker_params.freshness_bonus == 0.1


def test_retrieval_config_roundtrip_json():
    """모델 → JSON → 모델 라운드트립이 동일 값이어야 한다."""
    config = RetrievalConfig(
        default_retriever="vector",
        default_reranker="null",
        retriever_params=RetrieverParams(similarity_threshold=0.5),
    )
    dumped = config.model_dump()
    reloaded = RetrievalConfig.model_validate(dumped)
    assert reloaded.default_retriever == "vector"
    assert reloaded.default_reranker == "null"
    assert reloaded.retriever_params.similarity_threshold == 0.5


def test_retrieval_config_invalid_retriever_raises():
    with pytest.raises(Exception):
        RetrievalConfig.model_validate({"default_retriever": "bm25"})


def test_retrieval_config_invalid_reranker_raises():
    with pytest.raises(Exception):
        RetrievalConfig.model_validate({"default_reranker": "unknown_reranker"})


def test_retriever_params_out_of_range_raises():
    """범위를 벗어난 가중치는 유효성 오류를 발생시켜야 한다."""
    with pytest.raises(Exception):
        RetrieverParams(fts_weight=1.5)


# ── DB 기본값 JSONB와 파싱 호환성 ─────────────────────────────────────────────

def test_retrieval_config_parse_from_db_default_json():
    """DB DEFAULT JSONB 값을 파싱할 수 있어야 한다."""
    db_default_json = '{"default_retriever":"fts","retriever_params":{},"default_reranker":null,"reranker_params":{}}'
    raw = json.loads(db_default_json)
    config = RetrievalConfig.model_validate(raw)
    assert config.default_retriever == "fts"
    assert config.default_reranker is None


# ── SearchService._get_retrieval_config 폴백 동작 ────────────────────────────

def test_get_retrieval_config_fallback_on_empty_type():
    """document_type이 빈 문자열이면 기본 RetrievalConfig를 반환해야 한다."""
    from app.services.search_service import SearchService

    class FakeConn:
        pass

    service = SearchService()
    config = service._get_retrieval_config(FakeConn(), "")
    assert isinstance(config, RetrievalConfig)
    assert config.default_retriever == "fts"


def test_get_retrieval_config_fallback_on_db_error(monkeypatch):
    """DB 오류 시 기본 RetrievalConfig로 폴백해야 한다."""
    from app.services.search_service import SearchService

    class BadCursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): raise RuntimeError("DB down")

    class BadConn:
        def cursor(self, **kwargs): return BadCursor()

    service = SearchService()
    config = service._get_retrieval_config(BadConn(), "POLICY")
    assert isinstance(config, RetrievalConfig)


# ── UpdateDocumentTypeBody retrieval_config 필드 ─────────────────────────────

def test_update_document_type_body_accepts_retrieval_config():
    """UpdateDocumentTypeBody가 retrieval_config 필드를 수용해야 한다."""
    from app.api.v1.admin import UpdateDocumentTypeBody

    body = UpdateDocumentTypeBody(
        retrieval_config={
            "default_retriever": "hybrid",
            "retriever_params": {"fts_weight": 0.4, "vector_weight": 0.6, "similarity_threshold": 0.3},
            "default_reranker": "rule_based",
            "reranker_params": {},
        }
    )
    assert body.retrieval_config is not None
    assert body.retrieval_config["default_retriever"] == "hybrid"


def test_update_document_type_body_retrieval_config_optional():
    """retrieval_config 없이도 UpdateDocumentTypeBody를 생성할 수 있어야 한다."""
    from app.api.v1.admin import UpdateDocumentTypeBody

    body = UpdateDocumentTypeBody(display_name="새 이름")
    assert body.retrieval_config is None
    assert body.display_name == "새 이름"


# ── search.py retriever/reranker 파라미터 검증 (라우터 레벨) ─────────────────

def test_search_documents_accepts_retriever_reranker_params():
    """search_documents 함수 시그니처에 retriever/reranker 파라미터가 있어야 한다."""
    import inspect
    from app.api.v1.search import search_documents

    sig = inspect.signature(search_documents)
    assert "retriever" in sig.parameters
    assert "reranker" in sig.parameters


def test_search_documents_retriever_default_is_none():
    """retriever/reranker 파라미터의 기본값은 None이어야 한다."""
    import inspect
    from app.api.v1.search import search_documents

    sig = inspect.signature(search_documents)
    assert sig.parameters["retriever"].default is None
    assert sig.parameters["reranker"].default is None
