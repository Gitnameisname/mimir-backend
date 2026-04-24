"""FG 0-3 S16: 서비스 게이트 보강 (nodes_service + citation_reuse_service + prompt_builder).

services 79.30% → 80%+ 로 끌어올리기 위한 작은 서비스 묶음 테스트.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.nodes_service import NodesService, _to_response
from app.services.citation_reuse_service import CitationReuseService
from app.api.errors.exceptions import ApiNotFoundError


# ---------------------------------------------------------------------------
# nodes_service
# ---------------------------------------------------------------------------


def _mk_node(id_="n1", version_id="v1"):
    return SimpleNamespace(
        id=id_,
        version_id=version_id,
        parent_id=None,
        node_type="section",
        order_index=0,
        title="제목",
        content="본문",
        metadata={},
        created_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
    )


def test_to_response_maps_fields():
    node = _mk_node()
    resp = _to_response(node)
    assert resp.id == "n1"
    assert resp.version_id == "v1"
    assert resp.title == "제목"


def test_list_nodes_version_not_found_raises(monkeypatch):
    import app.services.nodes_service as ns_mod
    monkeypatch.setattr(
        ns_mod.versions_repository, "get_by_id", lambda conn, vid: None
    )
    svc = NodesService()
    with pytest.raises(ApiNotFoundError):
        svc.list_nodes(MagicMock(), "missing")


def test_list_nodes_returns_responses(monkeypatch):
    import app.services.nodes_service as ns_mod
    monkeypatch.setattr(
        ns_mod.versions_repository, "get_by_id",
        lambda conn, vid: SimpleNamespace(id=vid),
    )
    monkeypatch.setattr(
        ns_mod.nodes_repository, "list_by_version_id",
        lambda conn, vid: [_mk_node("n1"), _mk_node("n2")],
    )
    svc = NodesService()
    result = svc.list_nodes(MagicMock(), "v1")
    assert len(result) == 2


def test_get_node_version_not_found_raises(monkeypatch):
    import app.services.nodes_service as ns_mod
    monkeypatch.setattr(
        ns_mod.versions_repository, "get_by_id", lambda conn, vid: None
    )
    svc = NodesService()
    with pytest.raises(ApiNotFoundError, match="Version"):
        svc.get_node(MagicMock(), "v1", "n1")


def test_get_node_node_not_found_raises(monkeypatch):
    import app.services.nodes_service as ns_mod
    monkeypatch.setattr(
        ns_mod.versions_repository, "get_by_id",
        lambda conn, vid: SimpleNamespace(id=vid),
    )
    monkeypatch.setattr(
        ns_mod.nodes_repository, "get_by_id_and_version_id",
        lambda conn, nid, vid: None,
    )
    svc = NodesService()
    with pytest.raises(ApiNotFoundError, match="Node"):
        svc.get_node(MagicMock(), "v1", "n1")


def test_get_node_success(monkeypatch):
    import app.services.nodes_service as ns_mod
    monkeypatch.setattr(
        ns_mod.versions_repository, "get_by_id",
        lambda conn, vid: SimpleNamespace(id=vid),
    )
    monkeypatch.setattr(
        ns_mod.nodes_repository, "get_by_id_and_version_id",
        lambda conn, nid, vid: _mk_node(nid, vid),
    )
    svc = NodesService()
    resp = svc.get_node(MagicMock(), "v1", "n1")
    assert resp.id == "n1"


def test_singleton_exists():
    import app.services.nodes_service as ns_mod
    assert isinstance(ns_mod.nodes_service, NodesService)


# ---------------------------------------------------------------------------
# citation_reuse_service
# ---------------------------------------------------------------------------


def _mk_turn(citations=None):
    return SimpleNamespace(
        id="t1", conversation_id="c1", turn_number=1,
        user_message="q", assistant_response="a",
        retrieval_metadata={"citations": citations} if citations is not None else None,
        created_at=datetime.now(timezone.utc),
    )


def test_extract_cited_doc_ids_empty_turns():
    svc = CitationReuseService()
    assert svc.extract_cited_document_ids([]) == set()


def test_extract_cited_doc_ids_no_metadata():
    svc = CitationReuseService()
    turn = _mk_turn(citations=None)
    assert svc.extract_cited_document_ids([turn]) == set()


def test_extract_cited_doc_ids_happy():
    svc = CitationReuseService()
    turn = _mk_turn(citations=[
        {"document_id": "doc-1"},
        {"document_id": "doc-2"},
        {"document_id": "doc-1"},  # 중복 제거 확인
        {"other_field": "x"},  # document_id 없음 → skip
    ])
    result = svc.extract_cited_document_ids([turn])
    assert result == {"doc-1", "doc-2"}


def test_apply_citation_bonus_empty_results():
    svc = CitationReuseService()
    assert svc.apply_citation_bonus([], [_mk_turn()]) == []


def test_apply_citation_bonus_no_cited_ids():
    svc = CitationReuseService()
    results = [{"document_id": "doc-1", "score": 0.5}]
    # 이전 턴 없음 → 원본 그대로
    out = svc.apply_citation_bonus(results, [])
    assert out == results


def test_apply_citation_bonus_boosts_matching_docs():
    svc = CitationReuseService()
    results = [
        {"document_id": "doc-1", "score": 0.5},
        {"document_id": "doc-2", "score": 0.7},
        {"document_id": "doc-3", "score": 0.3},
    ]
    turns = [_mk_turn(citations=[{"document_id": "doc-1"}])]
    out = svc.apply_citation_bonus(results, turns)

    # doc-1 은 0.5 × 1.5 = 0.75 로 점수 상승 → 1위
    assert out[0]["document_id"] == "doc-1"
    assert out[0]["score"] == 0.75
    assert out[0]["citation_reused"] is True
    # doc-2 는 원래 0.7 유지 (2위)
    assert out[1]["document_id"] == "doc-2"


def test_get_reused_count():
    svc = CitationReuseService()
    results = [
        {"document_id": "doc-1", "score": 0.5},
        {"document_id": "doc-2", "score": 0.4},
    ]
    turns = [_mk_turn(citations=[{"document_id": "doc-1"}])]
    assert svc.get_reused_count(results, turns) == 1


def test_get_reused_count_no_overlap():
    svc = CitationReuseService()
    results = [{"document_id": "doc-999", "score": 0.5}]
    turns = [_mk_turn(citations=[{"document_id": "doc-1"}])]
    assert svc.get_reused_count(results, turns) == 0


# ---------------------------------------------------------------------------
# prompt_builder (간단히 모듈 로드 + 싱글턴 확인)
# ---------------------------------------------------------------------------


def test_prompt_builder_module_loads():
    """prompt_builder 모듈 import 검증 (기본 커버리지용)."""
    from app.services import prompt_builder
    assert prompt_builder is not None
