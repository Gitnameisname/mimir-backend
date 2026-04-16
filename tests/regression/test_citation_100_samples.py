"""
Citation 회귀 테스트 — 100개 샘플 문서 (Task 2-3)

모든 샘플 문서에 대해 Citation 생성 후 verify가 True여야 한다.
수정된 텍스트에 대해서는 verify가 False여야 한다.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.schemas.citation import Citation
from app.services.retrieval.citation_builder import CitationBuilder

# ── 100개 샘플 텍스트 ────────────────────────────────────────────────────────

SAMPLE_TEXTS = [
    f"샘플 문서 {i}: 이것은 Phase 2 회귀 테스트용 텍스트입니다. 내용 번호 {i}번이며, "
    f"Kubernetes, FastAPI, PostgreSQL 등 다양한 기술 키워드를 포함합니다."
    for i in range(100)
]


# ── 정상 케이스: verify = True ────────────────────────────────────────────────

@pytest.mark.parametrize("source_text", SAMPLE_TEXTS)
def test_citation_verify_true_for_sample(source_text: str):
    """생성된 citation이 같은 텍스트에 대해 verify=True를 반환해야 한다."""
    doc_id, ver_id, node_id = uuid4(), uuid4(), uuid4()
    citation = CitationBuilder.build(doc_id, ver_id, node_id, source_text)
    assert citation.verify(source_text) is True, (
        f"verify failed for text: {source_text[:40]}..."
    )


# ── 수정 감지: verify = False ─────────────────────────────────────────────────

@pytest.mark.parametrize("source_text", SAMPLE_TEXTS[:20])
def test_citation_verify_false_after_modification(source_text: str):
    """수정된 텍스트에 대해 verify=False를 반환해야 한다."""
    doc_id, ver_id, node_id = uuid4(), uuid4(), uuid4()
    citation = CitationBuilder.build(doc_id, ver_id, node_id, source_text)
    modified = source_text + " [내용 수정됨]"
    assert citation.verify(modified) is False, (
        f"verify should be False for modified text: {modified[:40]}..."
    )


# ── 해시 안정성 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source_text", SAMPLE_TEXTS[:10])
def test_citation_hash_stable_across_calls(source_text: str):
    """동일 텍스트에 대해 반복 생성해도 같은 hash가 나와야 한다."""
    doc_id, ver_id, node_id = uuid4(), uuid4(), uuid4()
    hashes = {
        CitationBuilder.build(doc_id, ver_id, node_id, source_text).content_hash
        for _ in range(5)
    }
    assert len(hashes) == 1, "hash is not deterministic"


# ── 멱등성: 동일 텍스트 → 동일 citation ─────────────────────────────────────

def test_same_text_same_hash_different_ids():
    """document_id/version_id가 달라도 같은 텍스트면 같은 hash여야 한다."""
    text = SAMPLE_TEXTS[0]
    c1 = Citation.from_chunk(uuid4(), uuid4(), uuid4(), text)
    c2 = Citation.from_chunk(uuid4(), uuid4(), uuid4(), text)
    assert c1.content_hash == c2.content_hash
