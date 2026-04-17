"""
SpanCalculator + SourceSpan 단위 테스트 — Phase 8 FG8.3 (task8-8).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.extraction_span import (
    ExtractedFieldWithAttribution,
    ExtractionResultWithAttribution,
    SourceSpan,
    SpanHighlight,
)
from app.services.extraction.span_calculator import (
    MultiSpanExtractor,
    SpanCalculator,
    SpanVisualizationConverter,
)


DOC_TEXT = "이 문서는 계약자 홍길동의 보험금 청구 관련 서류입니다. 청구 금액은 500만원이며, 사고 날짜는 2024-03-15입니다."


# ---------------------------------------------------------------------------
# SourceSpan 모델 검증
# ---------------------------------------------------------------------------

class TestSourceSpanModel:
    def test_valid_span(self):
        span = SourceSpan(
            document_id=uuid4(),
            span_offset=(5, 15),
            source_text="계약자 홍길동",
        )
        assert span.length == 10
        assert span.content_hash is not None

    def test_offset_start_equals_end_raises(self):
        with pytest.raises(Exception):
            SourceSpan(
                document_id=uuid4(),
                span_offset=(10, 10),
                source_text="x",
            )

    def test_offset_start_greater_than_end_raises(self):
        with pytest.raises(Exception):
            SourceSpan(
                document_id=uuid4(),
                span_offset=(20, 5),
                source_text="x",
            )

    def test_negative_start_raises(self):
        with pytest.raises(Exception):
            SourceSpan(
                document_id=uuid4(),
                span_offset=(-1, 5),
                source_text="x",
            )

    def test_hash_auto_computed(self):
        span = SourceSpan(
            document_id=uuid4(),
            span_offset=(0, 3),
            source_text="abc",
        )
        import hashlib
        expected = hashlib.sha256(b"abc").hexdigest()
        assert span.content_hash == expected

    def test_verify_against_document_success(self):
        text = "hello world"
        span = SourceSpan(
            document_id=uuid4(),
            span_offset=(6, 11),
            source_text="world",
        )
        assert span.verify_against_document(text) is True

    def test_verify_against_document_mismatch(self):
        text = "hello world"
        span = SourceSpan(
            document_id=uuid4(),
            span_offset=(0, 5),
            source_text="HELLO",
        )
        assert span.verify_against_document(text) is False

    def test_verify_against_document_out_of_range(self):
        span = SourceSpan(
            document_id=uuid4(),
            span_offset=(0, 100),
            source_text="x" * 100,
        )
        assert span.verify_against_document("short") is False


# ---------------------------------------------------------------------------
# SpanCalculator
# ---------------------------------------------------------------------------

class TestSpanCalculator:
    def setup_method(self):
        self.calc = SpanCalculator()

    def test_find_text_in_document_found(self):
        result = self.calc.find_text_in_document(DOC_TEXT, "홍길동")
        assert result is not None
        start, end = result
        assert DOC_TEXT[start:end] == "홍길동"

    def test_find_text_in_document_not_found(self):
        result = self.calc.find_text_in_document(DOC_TEXT, "없는텍스트")
        assert result is None

    def test_find_text_empty_search(self):
        assert self.calc.find_text_in_document(DOC_TEXT, "") is None

    def test_find_text_empty_document(self):
        assert self.calc.find_text_in_document("", "text") is None

    def test_find_all_occurrences(self):
        text = "abc abc abc"
        results = self.calc.find_all_occurrences(text, "abc")
        assert len(results) == 3
        for start, end in results:
            assert text[start:end] == "abc"

    def test_find_all_occurrences_none(self):
        assert self.calc.find_all_occurrences(DOC_TEXT, "없는텍스트") == []

    def test_verify_span_text_correct(self):
        result = self.calc.find_text_in_document(DOC_TEXT, "500만원")
        assert result is not None
        assert self.calc.verify_span_text(DOC_TEXT, result, "500만원") is True

    def test_verify_span_text_wrong(self):
        assert self.calc.verify_span_text(DOC_TEXT, (0, 3), "WRONG") is False

    def test_merge_overlapping_spans_empty(self):
        assert self.calc.merge_overlapping_spans([]) == []

    def test_merge_overlapping_spans_adjacent(self):
        spans = [(0, 5), (5, 10), (15, 20)]
        merged = self.calc.merge_overlapping_spans(spans)
        assert merged == [(0, 10), (15, 20)]

    def test_merge_overlapping_spans_contained(self):
        spans = [(0, 20), (5, 15)]
        merged = self.calc.merge_overlapping_spans(spans)
        assert merged == [(0, 20)]

    def test_create_source_span(self):
        doc_id = uuid4()
        span = self.calc.create_source_span(doc_id, DOC_TEXT, (0, 5))
        assert span is not None
        assert span.source_text == DOC_TEXT[0:5]
        assert span.document_id == doc_id

    def test_create_source_span_invalid_offset(self):
        doc_id = uuid4()
        span = self.calc.create_source_span(doc_id, DOC_TEXT, (100, 5))
        assert span is None

    def test_calculate_content_hash(self):
        h = self.calc.calculate_content_hash("test")
        import hashlib
        assert h == hashlib.sha256(b"test").hexdigest()

    def test_extract_spans_from_value_string(self):
        doc_id = uuid4()
        spans = self.calc.extract_spans_from_value(DOC_TEXT, doc_id, "name", "홍길동")
        assert len(spans) >= 1
        assert spans[0].source_text == "홍길동"

    def test_extract_spans_from_value_list(self):
        doc_id = uuid4()
        spans = self.calc.extract_spans_from_value(
            DOC_TEXT, doc_id, "items", ["홍길동", "500만원"]
        )
        assert len(spans) == 2

    def test_extract_spans_from_value_not_found(self):
        doc_id = uuid4()
        spans = self.calc.extract_spans_from_value(DOC_TEXT, doc_id, "x", "없는값")
        assert spans == []


# ---------------------------------------------------------------------------
# MultiSpanExtractor
# ---------------------------------------------------------------------------

class TestMultiSpanExtractor:
    def test_extract_multiple_fields(self):
        extractor = MultiSpanExtractor()
        doc_id = uuid4()
        fields = {"name": "홍길동", "amount": "500만원"}
        result = extractor.extract(DOC_TEXT, doc_id, fields)

        assert isinstance(result, ExtractionResultWithAttribution)
        name_field = result.get_field("name")
        assert name_field is not None
        assert len(name_field.source_spans) >= 1

    def test_extract_field_not_in_doc(self):
        extractor = MultiSpanExtractor()
        doc_id = uuid4()
        result = extractor.extract(DOC_TEXT, doc_id, {"unknown_field": "없는값"})
        field = result.get_field("unknown_field")
        assert field is not None
        assert len(field.source_spans) == 0

    def test_total_span_count(self):
        extractor = MultiSpanExtractor()
        doc_id = uuid4()
        result = extractor.extract(DOC_TEXT, doc_id, {"name": "홍길동", "date": "2024-03-15"})
        assert result.total_span_count == sum(len(f.source_spans) for f in result.fields)


# ---------------------------------------------------------------------------
# SpanVisualizationConverter
# ---------------------------------------------------------------------------

class TestSpanVisualizationConverter:
    def test_to_highlights_sorted(self):
        doc_id = uuid4()
        extractor = MultiSpanExtractor()
        result = extractor.extract(DOC_TEXT, doc_id, {"name": "홍길동", "amount": "500만원"})

        converter = SpanVisualizationConverter()
        highlights = converter.to_highlights(result)

        for h in highlights:
            assert isinstance(h, SpanHighlight)
        # 정렬 확인
        starts = [h.start for h in highlights]
        assert starts == sorted(starts)

    def test_to_highlight_dict(self):
        doc_id = uuid4()
        extractor = MultiSpanExtractor()
        result = extractor.extract(DOC_TEXT, doc_id, {"name": "홍길동"})
        converter = SpanVisualizationConverter()
        dicts = converter.to_highlight_dict(result)
        assert isinstance(dicts, list)
        for d in dicts:
            assert "start" in d
            assert "end" in d
            assert "field_name" in d
