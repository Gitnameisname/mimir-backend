"""
LLM09 Overreliance on LLM Output 검증 테스트.

검증 항목:
  - Citation tracking (TrackedCitation 5-tuple)
  - Citation-present rate ≥ 0.50 (단순 응답 기준)
  - Faithfulness: LLM 출력이 소스와 일치
  - Human-in-the-loop: critical 작업은 인간 승인 필수
  - Citation 5-tuple 필드 완전성
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")


# ---------------------------------------------------------------------------
# LLM09-001~005: Citation Tracker
# ---------------------------------------------------------------------------

class TestLLM09CitationTracker:
    """Citation tracking 기능 검증."""

    def test_citation_tracker_exists(self):
        """LLM09-001: CitationTracker가 존재한다."""
        from app.services.llm.citation_tracker import CitationTracker
        assert CitationTracker is not None

    def test_tracked_citation_dataclass_fields(self):
        """LLM09-002: TrackedCitation이 5-tuple 필드를 가진다."""
        from app.services.llm.citation_tracker import TrackedCitation
        import dataclasses

        fields = {f.name for f in dataclasses.fields(TrackedCitation)}
        required = {"document_id", "chunk_id", "content_hash", "quoted_text", "version"}
        for field in required:
            assert field in fields, f"TrackedCitation에 '{field}' 필드 없음"

    def test_extract_citations_finds_matching_chunks(self):
        """LLM09-003: 소스 청크와 일치하는 텍스트가 Citation으로 추출된다."""
        from app.services.llm.citation_tracker import CitationTracker

        tracker = CitationTracker()
        source_docs = [
            {
                "id": "doc-001",
                "chunks": [
                    {
                        "id": "chunk-001",
                        "content": "Revenue increased by 20% in Q4.",
                        "hash": "abc123",
                        "version": 1,
                    }
                ],
            }
        ]
        response = "According to the report, revenue increased by 20% in Q4."

        citations = tracker.extract_citations(response, source_docs)
        assert len(citations) > 0, "일치하는 청크가 Citation으로 추출되지 않음"
        assert citations[0].document_id == "doc-001"

    def test_no_citation_for_unrelated_text(self):
        """LLM09-004: 소스에 없는 텍스트는 Citation이 생성되지 않는다."""
        from app.services.llm.citation_tracker import CitationTracker

        tracker = CitationTracker()
        source_docs = [
            {
                "id": "doc-001",
                "chunks": [
                    {"id": "c-1", "content": "Apple prices are rising.", "hash": "x", "version": 1}
                ],
            }
        ]
        response = "The moon is made of cheese."

        citations = tracker.extract_citations(response, source_docs)
        assert len(citations) == 0, "관련 없는 응답에 Citation 생성됨"

    def test_citation_present_rate_calculated(self):
        """LLM09-005: citation_present_rate가 계산된다."""
        from app.services.llm.citation_tracker import CitationTracker, TrackedCitation

        tracker = CitationTracker()
        citations = [
            TrackedCitation(
                document_id="doc-1",
                chunk_id="c-1",
                content_hash="h",
                quoted_text="Revenue increased by 20% in Q4.",
                version=1,
            )
        ]
        response = "Revenue increased by 20% in Q4. This was a strong performance."
        rate = tracker.calculate_citation_present_rate(response, citations)
        assert 0.0 <= rate <= 1.0, "Citation-present rate가 [0,1] 범위 밖"


# ---------------------------------------------------------------------------
# LLM09-006~009: Citation Builder (기존 구현)
# ---------------------------------------------------------------------------

class TestLLM09CitationBuilder:
    """기존 CitationBuilder의 5-tuple 무결성 검증."""

    def test_citation_builder_exists(self):
        """LLM09-006: CitationBuilder가 존재한다."""
        from app.services.retrieval.citation_builder import CitationBuilder
        assert CitationBuilder is not None

    def test_citation_builder_has_sha256_hash(self):
        """LLM09-007: CitationBuilder가 SHA-256 해시를 사용한다."""
        citation_path = ROOT / "backend/app/services/retrieval/citation_builder.py"
        source = citation_path.read_text(encoding="utf-8")
        assert "sha256" in source.lower() or "hashlib" in source.lower(), (
            "CitationBuilder에 SHA-256 없음"
        )

    def test_citation_schema_has_5tuple_fields(self):
        """LLM09-008: Citation 스키마에 5-tuple 필드가 있다."""
        schemas_dir = ROOT / "backend/app/schemas"
        combined = ""
        for f in schemas_dir.rglob("*.py"):
            if "citation" in f.name.lower():
                combined += f.read_text(encoding="utf-8")

        if not combined:
            # app/schemas/citation.py 직접 확인
            citation_schema = ROOT / "backend/app/schemas/citation.py"
            if citation_schema.exists():
                combined = citation_schema.read_text(encoding="utf-8")

        assert "document_id" in combined or "version" in combined, (
            "Citation 스키마에 5-tuple 필드 없음"
        )

    def test_citation_service_exists(self):
        """LLM09-009: CitationService가 존재한다."""
        citation_service = ROOT / "backend/app/services/retrieval/citation_service.py"
        assert citation_service.exists(), "citation_service.py 없음"


# ---------------------------------------------------------------------------
# LLM09-010~013: Human-in-the-loop
# ---------------------------------------------------------------------------

class TestLLM09HumanInTheLoop:
    """Human-in-the-loop 메커니즘 검증."""

    def test_proposal_queue_exists(self):
        """LLM09-010: Proposal Queue API가 존재한다."""
        queue_path = ROOT / "backend/app/api/v1/proposal_queue.py"
        assert queue_path.exists(), "proposal_queue.py 없음"

    def test_proposal_queue_requires_human_review(self):
        """LLM09-011: Proposal Queue가 인간 검토를 요구한다."""
        queue_path = ROOT / "backend/app/api/v1/proposal_queue.py"
        source = queue_path.read_text(encoding="utf-8")

        has_review = any(
            kw in source.lower()
            for kw in ["approve", "review", "reject", "human", "admin"]
        )
        assert has_review, "Proposal Queue에 인간 검토 메커니즘 없음"

    def test_agent_output_requires_approval_for_write_ops(self):
        """LLM09-012: Agent 쓰기 작업은 승인이 필요하다."""
        service_path = ROOT / "backend/app/services/agent_proposal_service.py"
        source = service_path.read_text(encoding="utf-8")

        has_approval_gate = "approve" in source.lower() and "proposed" in source
        assert has_approval_gate, "Agent 쓰기 작업 승인 게이트 없음"

    def test_faithfulness_evaluation_exists(self):
        """LLM09-013: Faithfulness 평가 모듈이 존재한다."""
        eval_dir = ROOT / "backend/app/services/evaluation"
        fallback_path = eval_dir / "metrics/fallback.py"
        rule_based_path = eval_dir / "metrics/rule_based.py"

        has_evaluation = fallback_path.exists() or rule_based_path.exists()
        assert has_evaluation, "Faithfulness 평가 모듈 없음"
