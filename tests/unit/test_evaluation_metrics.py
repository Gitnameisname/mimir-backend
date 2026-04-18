"""
평가 메트릭 단위 테스트 — CitationPresent, HallucinationRate, ContextRecall, MetricRegistry.

검증 목표:
  - MetricScore 범위 검증 (0.0~1.0)
  - CitationPresentMetric: 인용문 있음/없음/빈 텍스트
  - HallucinationRateMetric: CPR의 보수(complement)
  - ContextRecallMetric: 소스 찾음/못찾음/비어있음
  - MetricRegistry: 등록/조회/일괄 계산
  - Citation 파서: Structured / Markdown 형식
  - CitationDetector 중복 제거
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")

from app.services.evaluation.metrics.citation import (
    Citation,
    CitationDetector,
    EntityType,
    MarkdownCitationParser,
    SourceType,
    StructuredCitationParser,
)
from app.services.evaluation.metrics.rule_based import (
    CitationPresentMetric,
    ContextRecallMetric,
    DEFAULT_REGISTRY,
    HallucinationRateMetric,
    MetricRegistry,
    MetricScore,
)


# ---------------------------------------------------------------------------
# MetricScore
# ---------------------------------------------------------------------------

class TestMetricScore:
    def test_valid_score(self):
        score = MetricScore(metric_name="test", score=0.75)
        assert score.score == 0.75

    def test_score_zero(self):
        score = MetricScore(metric_name="test", score=0.0)
        assert score.score == 0.0

    def test_score_one(self):
        score = MetricScore(metric_name="test", score=1.0)
        assert score.score == 1.0

    def test_score_out_of_range_raises(self):
        with pytest.raises(ValueError):
            MetricScore(metric_name="test", score=1.5)

    def test_score_negative_raises(self):
        with pytest.raises(ValueError):
            MetricScore(metric_name="test", score=-0.1)

    def test_details_stored(self):
        score = MetricScore(metric_name="test", score=0.5, details={"key": "value"})
        assert score.details["key"] == "value"


# ---------------------------------------------------------------------------
# StructuredCitationParser
# ---------------------------------------------------------------------------

class TestStructuredCitationParser:
    def test_parse_valid_citation(self):
        parser = StructuredCitationParser()
        text = "[CITE: entity_type=document, entity_value=doc1, source_type=database, source_id=src1, chunk_id=ch1]"
        citations = parser.parse(text)
        assert len(citations) == 1
        assert citations[0].source_id == "src1"
        assert citations[0].chunk_id == "ch1"

    def test_can_parse_true(self):
        parser = StructuredCitationParser()
        assert parser.can_parse("text [CITE: ...]") is True

    def test_can_parse_false(self):
        parser = StructuredCitationParser()
        assert parser.can_parse("plain text") is False

    def test_parse_multiple_citations(self):
        parser = StructuredCitationParser()
        text = (
            "[CITE: entity_type=document, entity_value=doc1, source_type=database, source_id=src1, chunk_id=ch1] "
            "[CITE: entity_type=chunk, entity_value=c2, source_type=file, source_id=src2, chunk_id=ch2]"
        )
        citations = parser.parse(text)
        assert len(citations) == 2

    def test_invalid_entity_type_skipped(self):
        parser = StructuredCitationParser()
        text = "[CITE: entity_type=invalid_type, entity_value=v, source_type=file, source_id=s, chunk_id=c]"
        citations = parser.parse(text)
        assert len(citations) == 0  # ValueError → continue


# ---------------------------------------------------------------------------
# MarkdownCitationParser
# ---------------------------------------------------------------------------

class TestMarkdownCitationParser:
    def test_parse_valid_markdown_citation(self):
        parser = MarkdownCitationParser()
        text = "[source doc](database://doc-123#chunk-456)"
        citations = parser.parse(text)
        assert len(citations) == 1
        assert citations[0].source_id == "doc-123"
        assert citations[0].chunk_id == "chunk-456"

    def test_can_parse_true(self):
        parser = MarkdownCitationParser()
        assert parser.can_parse("[link](url)") is True

    def test_can_parse_false(self):
        parser = MarkdownCitationParser()
        assert parser.can_parse("no markdown here") is False


# ---------------------------------------------------------------------------
# CitationDetector
# ---------------------------------------------------------------------------

class TestCitationDetector:
    def test_has_citation_with_structured_format(self):
        detector = CitationDetector()
        text = "[CITE: entity_type=document, entity_value=d, source_type=database, source_id=s1, chunk_id=c1]"
        assert detector.has_citation(text) is True

    def test_has_citation_with_markdown_format(self):
        detector = CitationDetector()
        text = "[doc](database://doc-1#chunk-1)"
        assert detector.has_citation(text) is True

    def test_no_citation(self):
        detector = CitationDetector()
        assert detector.has_citation("plain text without citations") is False

    def test_deduplication(self):
        """같은 source_id+chunk_id는 한 번만 카운트된다."""
        detector = CitationDetector()
        text = (
            "[CITE: entity_type=document, entity_value=d, source_type=database, source_id=s1, chunk_id=c1] "
            "[CITE: entity_type=document, entity_value=d2, source_type=database, source_id=s1, chunk_id=c1]"
        )
        citations = detector.detect_citations(text)
        assert len(citations) == 1


# ---------------------------------------------------------------------------
# CitationPresentMetric
# ---------------------------------------------------------------------------

class TestCitationPresentMetric:
    def test_empty_answer_returns_zero(self):
        metric = CitationPresentMetric()
        score = metric.compute(answer_text="")
        assert score.score == 0.0

    def test_whitespace_only_returns_zero(self):
        metric = CitationPresentMetric()
        score = metric.compute(answer_text="   \n  ")
        assert score.score == 0.0

    def test_all_sentences_cited(self):
        metric = CitationPresentMetric()
        answer = (
            "문서에 따르면 A이다. [CITE: entity_type=document, entity_value=d, "
            "source_type=database, source_id=s1, chunk_id=c1] "
            "또한 B이다. [doc](database://doc-1#chunk-1)"
        )
        score = metric.compute(answer_text=answer)
        assert score.score > 0.0

    def test_no_citations_returns_zero(self):
        metric = CitationPresentMetric()
        score = metric.compute(answer_text="This is an answer with no citations at all.")
        assert score.score == 0.0

    def test_details_keys_present(self):
        metric = CitationPresentMetric()
        score = metric.compute(answer_text="Some text.")
        assert "num_sentences" in score.details
        assert "cited_sentences" in score.details
        assert "uncited_sentences" in score.details

    def test_metric_name(self):
        assert CitationPresentMetric().metric_name == "citation_present_rate"


# ---------------------------------------------------------------------------
# HallucinationRateMetric
# ---------------------------------------------------------------------------

class TestHallucinationRateMetric:
    def test_complement_of_citation_present(self):
        metric = HallucinationRateMetric()
        score = metric.compute(answer_text="")
        # CPR=0.0 → hallucination=1.0
        assert score.score == pytest.approx(1.0)

    def test_no_hallucination_if_fully_cited(self):
        metric = HallucinationRateMetric()
        answer = "[CITE: entity_type=document, entity_value=d, source_type=database, source_id=s1, chunk_id=c1] 전부 인용."
        cpr = CitationPresentMetric()
        cpr_score = cpr.compute(answer_text=answer)

        hall = metric.compute(answer_text=answer)
        assert hall.score == pytest.approx(1.0 - cpr_score.score)

    def test_metric_name(self):
        assert HallucinationRateMetric().metric_name == "hallucination_rate"


# ---------------------------------------------------------------------------
# ContextRecallMetric
# ---------------------------------------------------------------------------

class TestContextRecallMetric:
    def test_empty_expected_returns_one(self):
        metric = ContextRecallMetric()
        score = metric.compute(expected_source_docs=[], retrieved_chunks=["s1"])
        assert score.score == 1.0

    def test_all_found(self):
        metric = ContextRecallMetric()
        score = metric.compute(
            expected_source_docs=["s1", "s2"],
            retrieved_chunks=[{"source_id": "s1"}, {"source_id": "s2"}],
        )
        assert score.score == 1.0

    def test_partial_found(self):
        metric = ContextRecallMetric()
        score = metric.compute(
            expected_source_docs=["s1", "s2", "s3"],
            retrieved_chunks=[{"source_id": "s1"}],
        )
        assert score.score == pytest.approx(1 / 3)

    def test_none_found(self):
        metric = ContextRecallMetric()
        score = metric.compute(
            expected_source_docs=["s1", "s2"],
            retrieved_chunks=[{"source_id": "s3"}],
        )
        assert score.score == 0.0

    def test_string_chunks(self):
        metric = ContextRecallMetric()
        score = metric.compute(
            expected_source_docs=["src1"],
            retrieved_chunks=["src1", "src2"],
        )
        assert score.score == 1.0

    def test_details_keys_present(self):
        metric = ContextRecallMetric()
        score = metric.compute(
            expected_source_docs=["s1"],
            retrieved_chunks=["s1"],
        )
        assert "expected_sources" in score.details
        assert "found_sources" in score.details
        assert "missing_sources" in score.details

    def test_metric_name(self):
        assert ContextRecallMetric().metric_name == "context_recall"


# ---------------------------------------------------------------------------
# MetricRegistry
# ---------------------------------------------------------------------------

class TestMetricRegistry:
    def test_register_and_get(self):
        registry = MetricRegistry()
        m = CitationPresentMetric()
        registry.register(m)
        assert registry.get("citation_present_rate") is m

    def test_list_metrics(self):
        registry = MetricRegistry()
        registry.register(CitationPresentMetric())
        registry.register(ContextRecallMetric())
        names = registry.list_metrics()
        assert "citation_present_rate" in names
        assert "context_recall" in names

    def test_unknown_metric_returns_none(self):
        registry = MetricRegistry()
        assert registry.get("nonexistent") is None

    def test_compute_all_unknown_skipped(self):
        registry = MetricRegistry()
        registry.register(CitationPresentMetric())
        results = registry.compute_all(["citation_present_rate", "nonexistent"], answer_text="")
        assert "citation_present_rate" in results
        assert "nonexistent" not in results

    def test_compute_all_returns_error_score_on_exception(self):
        """메트릭 계산 중 예외 발생 시 score=0.0 + error 상세를 반환한다."""
        registry = MetricRegistry()

        class BrokenMetric(CitationPresentMetric):
            def compute(self, **kwargs):
                raise RuntimeError("intentional failure")

        registry.register(BrokenMetric())
        results = registry.compute_all(["citation_present_rate"], answer_text="test")
        assert results["citation_present_rate"].score == 0.0
        assert "error" in results["citation_present_rate"].details

    def test_default_registry_has_required_metrics(self):
        """DEFAULT_REGISTRY에 3개 핵심 메트릭이 등록되어 있다."""
        names = DEFAULT_REGISTRY.list_metrics()
        assert "citation_present_rate" in names
        assert "hallucination_rate" in names
        assert "context_recall" in names
