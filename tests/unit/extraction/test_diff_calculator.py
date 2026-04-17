"""
DiffCalculator + ExtractionVerificationService 단위 테스트 — Phase 8 FG8.3 (task8-9).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models.extraction_record import (
    ExtractionRecord,
    MatchStatus,
    VerificationResult,
)
from app.services.extraction.diff_calculator import (
    DiffCalculator,
    SpanBasedDiffCalculator,
    _levenshtein_similarity,
    _span_iou,
)
from app.services.extraction.extraction_verification_service import ExtractionVerificationService


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_record(extracted_result=None) -> ExtractionRecord:
    return ExtractionRecord(
        extraction_candidate_id=uuid4(),
        document_id=uuid4(),
        extraction_schema_id="POLICY",
        extraction_schema_version=1,
        extraction_model="gpt-4",
        extracted_result=extracted_result or {"name": "홍길동", "amount": 500},
        extracted_timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Levenshtein similarity
# ---------------------------------------------------------------------------

class TestLevenshteinSimilarity:
    def test_identical_strings(self):
        assert _levenshtein_similarity("abc", "abc") == 1.0

    def test_completely_different(self):
        sim = _levenshtein_similarity("abc", "xyz")
        assert 0.0 <= sim < 1.0

    def test_empty_strings(self):
        assert _levenshtein_similarity("", "") == 1.0

    def test_one_empty(self):
        assert _levenshtein_similarity("abc", "") == 0.0

    def test_partial_match(self):
        sim = _levenshtein_similarity("홍길동", "홍길순")
        assert 0.5 < sim < 1.0


# ---------------------------------------------------------------------------
# Span IoU
# ---------------------------------------------------------------------------

class TestSpanIoU:
    def test_identical_spans(self):
        assert _span_iou((0, 10), (0, 10)) == 1.0

    def test_no_overlap(self):
        assert _span_iou((0, 5), (10, 20)) == 0.0

    def test_partial_overlap(self):
        iou = _span_iou((0, 10), (5, 15))
        assert 0.0 < iou < 1.0

    def test_contained_span(self):
        iou = _span_iou((0, 20), (5, 15))
        assert 0.0 < iou < 1.0


# ---------------------------------------------------------------------------
# DiffCalculator
# ---------------------------------------------------------------------------

class TestDiffCalculator:
    def setup_method(self):
        self.calc = DiffCalculator()

    def test_identical_dicts(self):
        status, diffs = self.calc.compare({"a": "x"}, {"a": "x"})
        assert status == MatchStatus.IDENTICAL
        assert all(d.match_type == "exact" for d in diffs)

    def test_complete_mismatch(self):
        status, diffs = self.calc.compare({"a": "hello"}, {"a": "world"})
        assert status in (MatchStatus.PARTIAL, MatchStatus.MISMATCH)

    def test_string_fuzzy_match(self):
        # 홍길동 → 홍길순 (유사)
        status, diffs = self.calc.compare({"name": "홍길동"}, {"name": "홍길순"})
        assert len(diffs) == 1
        assert diffs[0].similarity is not None
        assert diffs[0].similarity > 0.5

    def test_number_exact(self):
        status, diffs = self.calc.compare({"v": 3.14}, {"v": 3.14})
        assert status == MatchStatus.IDENTICAL

    def test_number_epsilon(self):
        status, diffs = self.calc.compare({"v": 1.0}, {"v": 1.0 + 1e-10})
        assert diffs[0].match_type == "exact"

    def test_number_different(self):
        status, diffs = self.calc.compare({"v": 100}, {"v": 200})
        assert diffs[0].similarity is not None
        assert diffs[0].similarity < 1.0

    def test_missing_key_in_new(self):
        status, diffs = self.calc.compare({"a": 1, "b": 2}, {"a": 1})
        missing = [d for d in diffs if d.match_type == "missing"]
        assert len(missing) == 1
        assert missing[0].field_name == "b"

    def test_extra_key_in_new(self):
        status, diffs = self.calc.compare({"a": 1}, {"a": 1, "b": 2})
        missing = [d for d in diffs if d.match_type == "missing"]
        assert len(missing) == 1

    def test_type_mismatch(self):
        status, diffs = self.calc.compare({"v": "abc"}, {"v": 123})
        assert diffs[0].match_type == "type_mismatch"

    def test_list_identical(self):
        status, diffs = self.calc.compare({"arr": [1, 2, 3]}, {"arr": [1, 2, 3]})
        assert diffs[0].match_type == "exact"

    def test_list_different_length(self):
        status, diffs = self.calc.compare({"arr": [1, 2]}, {"arr": [1, 2, 3]})
        assert diffs[0].match_type == "mismatch"

    def test_empty_dicts(self):
        status, diffs = self.calc.compare({}, {})
        assert status == MatchStatus.IDENTICAL

    def test_fields_filter(self):
        status, diffs = self.calc.compare(
            {"a": "x", "b": "y"},
            {"a": "x", "b": "z"},
            fields_filter=["a"],
        )
        assert all(d.field_name == "a" for d in diffs)

    def test_null_values_identical(self):
        status, diffs = self.calc.compare({"v": None}, {"v": None})
        assert diffs[0].match_type == "exact"

    def test_null_vs_value(self):
        status, diffs = self.calc.compare({"v": None}, {"v": "abc"})
        assert diffs[0].match_type == "mismatch"


# ---------------------------------------------------------------------------
# SpanBasedDiffCalculator
# ---------------------------------------------------------------------------

class TestSpanBasedDiffCalculator:
    def setup_method(self):
        self.calc = SpanBasedDiffCalculator()

    def test_identical_spans(self):
        assert self.calc.compare_spans([(0, 10)], [(0, 10)]) == 1.0

    def test_no_overlap(self):
        assert self.calc.compare_spans([(0, 5)], [(10, 20)]) == 0.0

    def test_partial_overlap(self):
        iou = self.calc.compare_spans([(0, 10)], [(5, 15)])
        assert 0.0 < iou < 1.0

    def test_empty_both(self):
        assert self.calc.compare_spans([], []) == 1.0

    def test_empty_one(self):
        assert self.calc.compare_spans([(0, 10)], []) == 0.0


# ---------------------------------------------------------------------------
# ExtractionVerificationService
# ---------------------------------------------------------------------------

class TestExtractionVerificationService:
    def setup_method(self):
        self.svc = ExtractionVerificationService()

    def test_verify_identical(self):
        record = _make_record({"name": "홍길동", "amount": 500})
        vr = self.svc.verify(record, {"name": "홍길동", "amount": 500})
        assert vr.match_status == MatchStatus.IDENTICAL
        assert vr.field_accuracy == 1.0

    def test_verify_mismatch(self):
        record = _make_record({"name": "홍길동"})
        vr = self.svc.verify(record, {"name": "이순신"})
        assert vr.match_status in (MatchStatus.PARTIAL, MatchStatus.MISMATCH)
        assert vr.field_accuracy < 1.0

    def test_verify_partial(self):
        record = _make_record({"name": "홍길동", "amount": 500})
        vr = self.svc.verify(record, {"name": "홍길동", "amount": 9999})
        assert vr.match_status in (MatchStatus.PARTIAL, MatchStatus.MISMATCH)

    def test_build_audit_trail(self):
        record = _make_record()
        vr = self.svc.verify(record, {"name": "홍길동", "amount": 500})
        trail = self.svc.build_audit_trail(record, [vr])
        assert "extraction_candidate_id" in trail
        assert "verifications" in trail
        assert trail["verification_count"] == 1

    def test_compute_document_hash(self):
        h = ExtractionVerificationService.compute_document_hash("hello")
        import hashlib
        assert h == hashlib.sha256(b"hello").hexdigest()

    def test_verify_with_fields_filter(self):
        record = _make_record({"name": "홍길동", "amount": 500})
        vr = self.svc.verify(record, {"name": "홍길동", "amount": 9999},
                              fields_to_verify=["name"])
        assert vr.field_accuracy == 1.0
