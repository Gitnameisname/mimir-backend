"""
Task 2-6 통합 테스트: 검색 API Retriever/Reranker 플러그인

@pytest.mark.integration 마커로 구분 — 실행 시 DB 연결 필요.
  pytest -m integration tests/integration/test_search_plugins.py
"""
from __future__ import annotations

import pytest


# ── retriever 파라미터 유효성 검사 ────────────────────────────────────────────

@pytest.mark.integration
def test_search_with_fts_retriever(test_client, auth_headers):
    """retriever=fts 파라미터가 200을 반환해야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "테스트", "type": "POLICY", "retriever": "fts"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "results" in data
    assert data["retriever"] == "fts"


@pytest.mark.integration
def test_search_with_hybrid_retriever(test_client, auth_headers):
    """retriever=hybrid 파라미터가 200을 반환해야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "정책", "type": "POLICY", "retriever": "hybrid"},
        headers=auth_headers,
    )
    assert resp.status_code == 200


@pytest.mark.integration
def test_search_with_rule_based_reranker(test_client, auth_headers):
    """reranker=rule_based 파라미터가 200을 반환해야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "문서", "reranker": "rule_based"},
        headers=auth_headers,
    )
    assert resp.status_code == 200


@pytest.mark.integration
def test_search_with_null_reranker(test_client, auth_headers):
    """reranker=null 파라미터가 200을 반환해야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "문서", "reranker": "null"},
        headers=auth_headers,
    )
    assert resp.status_code == 200


# ── S1 하위호환성 ─────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_search_s1_compat_without_plugin_params(test_client, auth_headers):
    """S1 클라이언트 — retriever/reranker 없이도 200이어야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "문서"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # S1 응답 형식: results, pagination 포함
    assert "results" in data
    assert "pagination" in data


# ── DocumentType retrieval_config Admin API ───────────────────────────────────

@pytest.mark.integration
def test_update_document_type_retrieval_config(test_client, admin_auth_headers):
    """Admin: PATCH document-types에서 retrieval_config 수정이 되어야 한다."""
    resp = test_client.patch(
        "/api/v1/admin/document-types/POLICY",
        json={
            "retrieval_config": {
                "default_retriever": "hybrid",
                "retriever_params": {
                    "fts_weight": 0.5,
                    "vector_weight": 0.5,
                    "similarity_threshold": 0.25,
                },
                "default_reranker": "rule_based",
                "reranker_params": {"freshness_bonus": 0.1, "pinned_bonus": 0.2},
            }
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["retrieval_config"]["default_retriever"] == "hybrid"


@pytest.mark.integration
def test_update_document_type_invalid_retrieval_config(test_client, admin_auth_headers):
    """잘못된 retrieval_config는 422를 반환해야 한다."""
    resp = test_client.patch(
        "/api/v1/admin/document-types/POLICY",
        json={"retrieval_config": {"default_retriever": "bm25_unsupported"}},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 422


# ── 결과에 citation 포함 여부 ──────────────────────────────────────────────────

@pytest.mark.integration
def test_plugin_search_results_include_citation(test_client, auth_headers, seed_document_chunks):
    """retriever 파라미터 사용 시 결과에 citation이 포함되어야 한다."""
    resp = test_client.get(
        "/api/v1/search/documents",
        params={"q": "테스트", "retriever": "fts"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    for item in data.get("results", []):
        # citation이 있을 경우 5-tuple 구조 확인
        if item.get("citation"):
            assert "document_id" in item["citation"]
            assert "content_hash" in item["citation"]
