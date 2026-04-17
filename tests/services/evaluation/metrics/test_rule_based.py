"""
규칙 기반 평가 지표 단위 테스트 — Phase 7 FG7.2 (task7-4)

테스트 범위:
- CitationDetector: structured + markdown 형식 감지, 중복 제거
- SentenceSplitter: 기본 분할, 약자 예외, 최소 길이, 줄바꿈
- CitationPresentMetric: 0.0/0.5/1.0 케이스, 공백 입력
- HallucinationRateMetric: CPR 역수 관계
- ContextRecallMetric: full/partial/none/empty 기대 소스
- MetricRegistry: 등록, compute_all, 미지 지표 처리
"""
from __future__ import annotations

import pytest

from app.services.evaluation.metrics import (
    CitationDetector,
    CitationPresentMetric,
    ContextRecallMetric,
    HallucinationRateMetric,
    MetricRegistry,
    MetricScore,
    SentenceSplitter,
    StructuredCitationParser,
    MarkdownCitationParser,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CITE = (
    "[CITE: entity_type=chunk, entity_value=info, "
    "source_type=file, source_id=doc1, chunk_id=ch1]"
)
_CITE2 = (
    "[CITE: entity_type=chunk, entity_value=other, "
    "source_type=file, source_id=doc2, chunk_id=ch2]"
)
_MD_CITE = "[info](file://doc1#ch1)"


# ---------------------------------------------------------------------------
# CitationDetector
# ---------------------------------------------------------------------------

class TestCitationDetector:
    def setup_method(self):
        self.det = CitationDetector()

    def test_structured_detected(self):
        cites = self.det.detect_citations(f"Some text {_CITE}")
        assert len(cites) == 1
        assert cites[0].source_id == "doc1"
        assert cites[0].chunk_id == "ch1"

    def test_markdown_detected(self):
        cites = self.det.detect_citations(f"Text {_MD_CITE} here")
        assert len(cites) == 1
        assert cites[0].source_id == "doc1"

    def test_multiple_structured(self):
        cites = self.det.detect_citations(f"A {_CITE} B {_CITE2}")
        assert len(cites) == 2

    def test_deduplication(self):
        cites = self.det.detect_citations(f"{_CITE} again {_CITE}")
        assert len(cites) == 1

    def test_has_citation_true(self):
        assert self.det.has_citation(f"text {_CITE}")

    def test_has_citation_false(self):
        assert not self.det.has_citation("plain text no citation")

    def test_no_citation(self):
        assert self.det.detect_citations("no cite here") == []

    def test_invalid_entity_type_skipped(self):
        bad = "[CITE: entity_type=UNKNOWN, entity_value=x, source_type=file, source_id=d, chunk_id=c]"
        cites = self.det.detect_citations(bad)
        assert cites == []


# ---------------------------------------------------------------------------
# SentenceSplitter
# ---------------------------------------------------------------------------

class TestSentenceSplitter:
    def test_basic_split_by_newline(self):
        splitter = SentenceSplitter(split_by_newline=True, min_length=1)
        sents = splitter.split("Line one\nLine two\nLine three")
        assert len(sents) == 3

    def test_empty_input(self):
        sents = SentenceSplitter().split("")
        assert sents == []

    def test_whitespace_only(self):
        sents = SentenceSplitter().split("   \n  ")
        assert sents == []

    def test_min_length_filters_short(self):
        splitter = SentenceSplitter(split_by_newline=True, min_length=10)
        sents = splitter.split("Hi\nThis is longer than ten chars")
        texts = [s.text for s in sents]
        assert all(len(t) >= 10 for t in texts)
        assert any("longer" in t for t in texts)

    def test_period_split(self):
        splitter = SentenceSplitter(split_by_newline=False, min_length=3)
        sents = splitter.split("Hello world. Goodbye world.")
        assert len(sents) == 2

    def test_exclamation_split(self):
        splitter = SentenceSplitter(split_by_newline=False, min_length=3)
        sents = splitter.split("Wow! Great.")
        assert len(sents) == 2

    def test_question_split(self):
        splitter = SentenceSplitter(split_by_newline=False, min_length=3)
        sents = splitter.split("Really? Yes.")
        assert len(sents) == 2

    def test_abbreviation_no_split(self):
        splitter = SentenceSplitter(split_by_newline=False, min_length=3)
        sents = splitter.split("The U.S. is large. Next sentence.")
        # U.S. should not trigger a split
        assert any("U.S." in s.text for s in sents)

    def test_sentence_text_preserved(self):
        splitter = SentenceSplitter(split_by_newline=True, min_length=1)
        sents = splitter.split("Hello\nWorld")
        assert sents[0].text == "Hello"
        assert sents[1].text == "World"


# ---------------------------------------------------------------------------
# CitationPresentMetric
# ---------------------------------------------------------------------------

class TestCitationPresentMetric:
    def setup_method(self):
        self.metric = CitationPresentMetric(SentenceSplitter(split_by_newline=True, min_length=1))

    def test_empty_answer_zero(self):
        result = self.metric.compute(answer_text="")
        assert result.score == 0.0
        assert result.details["num_sentences"] == 0

    def test_all_cited(self):
        answer = f"Sentence one {_CITE}\nSentence two {_CITE2}"
        result = self.metric.compute(answer_text=answer)
        assert result.score == 1.0
        assert result.details["cited_sentences"] == 2

    def test_none_cited(self):
        answer = "No citations here\nOr here either"
        result = self.metric.compute(answer_text=answer)
        assert result.score == 0.0
        assert result.details["uncited_sentences"] == 2

    def test_half_cited(self):
        answer = f"Cited line {_CITE}\nUncited line"
        result = self.metric.compute(answer_text=answer)
        assert result.score == pytest.approx(0.5)

    def test_markdown_citation_counts(self):
        answer = f"Has markdown {_MD_CITE}\nNo citation"
        result = self.metric.compute(answer_text=answer)
        assert result.score == pytest.approx(0.5)

    def test_single_cited_sentence(self):
        result = self.metric.compute(answer_text=f"Only one line {_CITE}")
        assert result.score == 1.0

    def test_details_keys_present(self):
        result = self.metric.compute(answer_text=f"line {_CITE}")
        assert "num_sentences" in result.details
        assert "cited_sentences" in result.details
        assert "uncited_sentences" in result.details

    def test_metric_name(self):
        assert self.metric.metric_name == "citation_present_rate"


# ---------------------------------------------------------------------------
# HallucinationRateMetric
# ---------------------------------------------------------------------------

class TestHallucinationRateMetric:
    def setup_method(self):
        self.metric = HallucinationRateMetric(
            SentenceSplitter(split_by_newline=True, min_length=1)
        )

    def test_no_hallucination(self):
        answer = f"All cited {_CITE}\nAlso cited {_CITE2}"
        result = self.metric.compute(answer_text=answer)
        assert result.score == 0.0

    def test_full_hallucination(self):
        result = self.metric.compute(answer_text="No citations\nAt all")
        assert result.score == 1.0

    def test_partial_hallucination(self):
        answer = f"Cited {_CITE}\nNot cited"
        result = self.metric.compute(answer_text=answer)
        assert result.score == pytest.approx(0.5)

    def test_complements_citation_present(self):
        cpm = CitationPresentMetric(SentenceSplitter(split_by_newline=True, min_length=1))
        answer = f"A {_CITE}\nB"
        cpr = cpm.compute(answer_text=answer).score
        hr = self.metric.compute(answer_text=answer).score
        assert cpr + hr == pytest.approx(1.0)

    def test_metric_name(self):
        assert self.metric.metric_name == "hallucination_rate"

    def test_empty_answer(self):
        result = self.metric.compute(answer_text="")
        assert result.score == 1.0


# ---------------------------------------------------------------------------
# ContextRecallMetric
# ---------------------------------------------------------------------------

class TestContextRecallMetric:
    def setup_method(self):
        self.metric = ContextRecallMetric()

    def test_full_recall(self):
        result = self.metric.compute(
            expected_source_docs=["d1", "d2"],
            retrieved_chunks=[{"source_id": "d1"}, {"source_id": "d2"}],
        )
        assert result.score == 1.0

    def test_partial_recall(self):
        result = self.metric.compute(
            expected_source_docs=["d1", "d2"],
            retrieved_chunks=[{"source_id": "d1"}],
        )
        assert result.score == pytest.approx(0.5)
        assert "d2" in result.details["missing_sources"]

    def test_zero_recall(self):
        result = self.metric.compute(
            expected_source_docs=["d1", "d2"],
            retrieved_chunks=[{"source_id": "d3"}],
        )
        assert result.score == 0.0

    def test_empty_expected_returns_one(self):
        result = self.metric.compute(
            expected_source_docs=[],
            retrieved_chunks=[{"source_id": "d1"}],
        )
        assert result.score == 1.0

    def test_duplicate_retrieved_deduplicated(self):
        result = self.metric.compute(
            expected_source_docs=["d1"],
            retrieved_chunks=[{"source_id": "d1"}, {"source_id": "d1"}],
        )
        assert result.score == 1.0

    def test_string_chunks_accepted(self):
        result = self.metric.compute(
            expected_source_docs=["d1"],
            retrieved_chunks=["d1"],
        )
        assert result.score == 1.0

    def test_missing_sources_in_details(self):
        result = self.metric.compute(
            expected_source_docs=["d1", "d2"],
            retrieved_chunks=[],
        )
        assert set(result.details["missing_sources"]) == {"d1", "d2"}

    def test_metric_name(self):
        assert self.metric.metric_name == "context_recall"


# ---------------------------------------------------------------------------
# MetricScore
# ---------------------------------------------------------------------------

class TestMetricScore:
    def test_valid_score(self):
        ms = MetricScore(metric_name="test", score=0.75)
        assert ms.score == 0.75

    def test_boundary_zero(self):
        ms = MetricScore(metric_name="test", score=0.0)
        assert ms.score == 0.0

    def test_boundary_one(self):
        ms = MetricScore(metric_name="test", score=1.0)
        assert ms.score == 1.0

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            MetricScore(metric_name="test", score=1.1)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            MetricScore(metric_name="test", score=-0.1)


# ---------------------------------------------------------------------------
# MetricRegistry
# ---------------------------------------------------------------------------

class TestMetricRegistry:
    def test_register_and_get(self):
        reg = MetricRegistry()
        m = CitationPresentMetric()
        reg.register(m)
        assert reg.get("citation_present_rate") is m

    def test_list_metrics(self):
        reg = MetricRegistry()
        reg.register(CitationPresentMetric())
        reg.register(ContextRecallMetric())
        names = reg.list_metrics()
        assert "citation_present_rate" in names
        assert "context_recall" in names

    def test_compute_all(self):
        reg = MetricRegistry()
        reg.register(HallucinationRateMetric())
        results = reg.compute_all(
            ["hallucination_rate"],
            answer_text="Some text",
        )
        assert "hallucination_rate" in results
        assert 0.0 <= results["hallucination_rate"].score <= 1.0

    def test_unknown_metric_skipped(self):
        reg = MetricRegistry()
        results = reg.compute_all(["nonexistent_metric"])
        assert "nonexistent_metric" not in results

    def test_get_missing_returns_none(self):
        reg = MetricRegistry()
        assert reg.get("missing") is None
