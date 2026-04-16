"""
Citation 5-tuple 단위 테스트 — Task 2-1
"""
from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from app.schemas.citation import Citation
from app.services.retrieval.citation_builder import CitationBuilder, _NIL_NODE_ID

# ── 공통 픽스처 ─────────────────────────────────────────────────────────────

DOC_ID = uuid4()
VER_ID = uuid4()
NODE_ID = uuid4()
SOURCE = "Kubernetes는 컨테이너 오케스트레이션 플랫폼입니다."


# ── Citation.from_chunk ────────────────────────────────────────────────────

def test_citation_from_chunk_produces_correct_hash():
    """SHA-256 계산이 표준 라이브러리 결과와 일치해야 한다."""
    citation = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    expected = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    assert citation.content_hash == expected


def test_citation_hash_length_always_64():
    """content_hash는 항상 64자(SHA-256 hex)여야 한다."""
    for text in ["a", "a" * 1000, "한글", "🎉"]:
        c = Citation.from_chunk(uuid4(), uuid4(), uuid4(), text)
        assert len(c.content_hash) == 64


def test_citation_verify_returns_true_for_original():
    citation = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    assert citation.verify(SOURCE) is True


def test_citation_verify_returns_false_after_modification():
    citation = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    assert citation.verify(SOURCE + " (수정됨)") is False


def test_citation_is_frozen():
    """Citation 모델은 불변(frozen)이어야 한다."""
    citation = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    with pytest.raises(Exception):
        citation.content_hash = "tampered"  # type: ignore[misc]


def test_citation_span_offset_stored():
    c = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE, span_offset=42)
    assert c.span_offset == 42


def test_citation_span_offset_none_by_default():
    c = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    assert c.span_offset is None


def test_citation_span_offset_negative_rejected():
    with pytest.raises(Exception):
        Citation(
            document_id=DOC_ID,
            version_id=VER_ID,
            node_id=NODE_ID,
            span_offset=-1,
            content_hash="a" * 64,
        )


def test_citation_json_roundtrip():
    citation = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE)
    restored = Citation.model_validate_json(citation.model_dump_json())
    assert restored == citation


def test_citation_from_chunk_empty_source_raises():
    with pytest.raises(ValueError, match="source_text must not be empty"):
        Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, "")


def test_citation_hash_stability():
    """동일 입력은 항상 동일 해시를 생성해야 한다."""
    hashes = {
        Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE).content_hash
        for _ in range(10)
    }
    assert len(hashes) == 1


def test_citation_model_dump_includes_all_fields():
    c = Citation.from_chunk(DOC_ID, VER_ID, NODE_ID, SOURCE, span_offset=10)
    d = c.model_dump()
    assert all(
        k in d
        for k in ["document_id", "version_id", "node_id", "span_offset", "content_hash"]
    )


# ── CitationBuilder ────────────────────────────────────────────────────────

def test_citation_builder_build():
    citation = CitationBuilder.build(DOC_ID, VER_ID, NODE_ID, SOURCE)
    assert citation.document_id == DOC_ID
    assert citation.version_id == VER_ID
    assert citation.node_id == NODE_ID


def test_citation_builder_empty_source_raises():
    with pytest.raises(ValueError, match="source_text must not be empty"):
        CitationBuilder.build(DOC_ID, VER_ID, NODE_ID, "")


def test_citation_builder_node_id_none_uses_nil_uuid():
    """node_id가 None이면 nil UUID로 대체해야 한다."""
    citation = CitationBuilder.build(DOC_ID, VER_ID, None, SOURCE)
    assert citation.node_id == _NIL_NODE_ID


def test_citation_builder_str_ids():
    """str 타입 ID도 정상 처리해야 한다."""
    citation = CitationBuilder.build(
        str(DOC_ID), str(VER_ID), str(NODE_ID), SOURCE
    )
    assert citation.document_id == DOC_ID


def test_citation_builder_from_retrieved_chunk():
    """S1 RetrievedChunk 유사 객체에서 Citation 생성."""
    from dataclasses import dataclass
    from typing import Optional as Opt

    @dataclass
    class FakeChunk:
        document_id: str = str(DOC_ID)
        version_id: str = str(VER_ID)
        node_id: Opt[str] = str(NODE_ID)
        source_text: str = SOURCE

    citation = CitationBuilder.from_retrieved_chunk(FakeChunk())
    assert citation.document_id == DOC_ID
    assert citation.verify(SOURCE) is True


def test_citation_builder_from_chunk_node_id_none():
    """node_id=None 청크는 nil UUID로 대체되어야 한다."""
    from dataclasses import dataclass
    from typing import Optional as Opt

    @dataclass
    class FakeChunkNoNode:
        document_id: str = str(DOC_ID)
        version_id: str = str(VER_ID)
        node_id: Opt[str] = None
        source_text: str = SOURCE

    citation = CitationBuilder.from_retrieved_chunk(FakeChunkNoNode())
    assert citation.node_id == _NIL_NODE_ID


def test_citation_builder_from_chunk_missing_attr_raises():
    """필수 속성 없는 객체는 ValueError를 발생시켜야 한다."""

    class BadChunk:
        pass

    with pytest.raises(ValueError, match="document_id"):
        CitationBuilder.from_retrieved_chunk(BadChunk())


# ── 인코딩 다양성 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Hello, World!",
    "안녕하세요 세계",
    "こんにちは世界",
    "Привет мир",
    "  공백 포함  ",
])
def test_sha256_matches_stdlib_for_various_encodings(text: str):
    """다양한 유니코드 텍스트에서 SHA-256이 stdlib 결과와 일치해야 한다."""
    c = Citation.from_chunk(uuid4(), uuid4(), uuid4(), text)
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert c.content_hash == expected
