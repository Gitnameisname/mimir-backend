"""
S1 ↔ S2 검색 결과 호환성 테스트 (Task 2-3)

S1 클라이언트: citation 필드 없는 응답을 정상 파싱
S2 클라이언트: citation 필드 있는 응답을 활용
DB 의존 없음 — 순수 Pydantic 스키마 수준 테스트.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest

from app.schemas.citation import Citation
from app.schemas.search import DocumentSearchResult, DocumentSnippet


_DOC_ID = str(uuid4())
_VER_ID = str(uuid4())
_NODE_ID = str(uuid4())
_HASH = "a" * 64


def _make_s1_result(**kwargs) -> dict:
    """S1 검색 결과 딕셔너리 (citation 없음)."""
    base = {
        "id": _DOC_ID,
        "title": "테스트 문서",
        "document_type": "POLICY",
        "status": "published",
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    base.update(kwargs)
    return base


# ── S1 클라이언트: citation 없는 응답 파싱 ───────────────────────────────────

def test_s1_result_without_citation_is_valid():
    """citation 필드 없는 검색 결과도 유효해야 한다 (S1 하위호환)."""
    result = DocumentSearchResult(**_make_s1_result())
    assert result.citation is None


def test_s1_result_fields_unchanged():
    """S1 핵심 필드(id, title, document_type, status)가 그대로 존재해야 한다."""
    result = DocumentSearchResult(**_make_s1_result())
    assert result.id == _DOC_ID
    assert result.title == "테스트 문서"
    assert result.document_type == "POLICY"
    assert result.status == "published"


def test_s1_result_rank_default_zero():
    result = DocumentSearchResult(**_make_s1_result())
    assert result.rank == 0.0


def test_s1_result_snippets_default_empty():
    result = DocumentSearchResult(**_make_s1_result())
    assert result.snippets == []


# ── S2 클라이언트: citation 필드 활용 ────────────────────────────────────────

def test_s2_result_with_citation():
    """S2 응답에서 citation 5-tuple을 정상 파싱해야 한다."""
    citation = Citation(
        document_id=UUID(_DOC_ID),
        version_id=UUID(_VER_ID),
        node_id=UUID(_NODE_ID),
        content_hash=_HASH,
    )
    result = DocumentSearchResult(
        **_make_s1_result(),
        citation=citation,
    )
    assert result.citation is not None
    assert result.citation.content_hash == _HASH


def test_s2_citation_verify():
    """S2 citation이 verify 메서드를 정상 호출해야 한다."""
    source = "테스트 원문"
    citation = Citation.from_chunk(
        UUID(_DOC_ID), UUID(_VER_ID), UUID(_NODE_ID), source
    )
    result = DocumentSearchResult(
        **_make_s1_result(),
        citation=citation,
    )
    assert result.citation.verify(source) is True
    assert result.citation.verify(source + "수정") is False


# ── 직렬화 하위호환 ──────────────────────────────────────────────────────────

def test_s2_result_json_includes_citation():
    """S2 응답의 JSON에 citation 필드가 포함되어야 한다."""
    citation = Citation(
        document_id=UUID(_DOC_ID),
        version_id=UUID(_VER_ID),
        node_id=UUID(_NODE_ID),
        content_hash=_HASH,
    )
    result = DocumentSearchResult(**_make_s1_result(), citation=citation)
    d = result.model_dump()
    assert "citation" in d
    assert d["citation"]["content_hash"] == _HASH


def test_s1_result_json_citation_is_none():
    """S1 응답의 JSON에서 citation은 None이어야 한다."""
    result = DocumentSearchResult(**_make_s1_result())
    d = result.model_dump()
    assert d["citation"] is None


# ── Citation 5-tuple 직접 파싱 ───────────────────────────────────────────────

def test_citation_model_validate_from_dict():
    """dict에서 Citation을 직접 파싱할 수 있어야 한다 (S2 클라이언트)."""
    raw = {
        "document_id": _DOC_ID,
        "version_id": _VER_ID,
        "node_id": _NODE_ID,
        "span_offset": None,
        "content_hash": _HASH,
    }
    citation = Citation.model_validate(raw)
    assert citation.document_id == UUID(_DOC_ID)
    assert citation.span_offset is None
