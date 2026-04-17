"""
ExtractionEvaluator + QualityGateChecker 단위 테스트 — Phase 8 FG8.3 (task8-10).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models.extraction_evaluation import (
    ExpectedField,
    ExpectedSpan,
    ExtractionMetrics,
    GoldenExtractionItem,
    GoldenExtractionSet,
    QualityGateCheckRequest,
)
from app.services.extraction.extraction_evaluator import (
    EvaluationReportGenerator,
    ExtractionEvaluator,
    QualityGateChecker,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_item(
    fields: list = None,
    spans: list = None,
    document_type: str = "POLICY",
) -> GoldenExtractionItem:
    if fields is None:
        fields = [
            ExpectedField(field_name="name", expected_value="홍길동", field_type="string", required=True),
            ExpectedField(field_name="amount", expected_value=500, field_type="integer", required=True),
        ]
    return GoldenExtractionItem(
        document_id=uuid4(),
        document_type=document_type,
        expected_fields=fields,
        expected_spans=spans or [],
    )


def _make_eval_result(
    field_accuracy=1.0,
    span_accuracy=1.0,
    required_field_coverage=1.0,
    type_correctness=1.0,
    overall_score=1.0,
):
    from app.models.extraction_evaluation import ExtractionEvaluationResult
    return ExtractionEvaluationResult(
        metrics=ExtractionMetrics(
            field_accuracy=field_accuracy,
            span_accuracy=span_accuracy,
            required_field_coverage=required_field_coverage,
            type_correctness=type_correctness,
            overall_score=overall_score,
        ),
        evaluated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# ExtractionEvaluator
# ---------------------------------------------------------------------------

class TestExtractionEvaluator:
    def setup_method(self):
        self.evaluator = ExtractionEvaluator()

    def test_perfect_match(self):
        item = _make_item()
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
        )
        assert result.metrics.field_accuracy == 1.0
        assert result.metrics.type_correctness == 1.0

    def test_complete_mismatch(self):
        item = _make_item()
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "이순신", "amount": 9999},
        )
        assert result.metrics.field_accuracy < 1.0

    def test_missing_required_field(self):
        item = _make_item()
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동"},  # amount 누락
        )
        # required_field_coverage < 1 (amount is None)
        assert result.metrics.required_field_coverage < 1.0

    def test_type_mismatch_reduces_type_correctness(self):
        item = _make_item(
            fields=[ExpectedField(field_name="amount", expected_value=500,
                                  field_type="integer", required=True)]
        )
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"amount": "500"},  # string instead of int
        )
        assert result.metrics.type_correctness < 1.0

    def test_span_accuracy_computed(self):
        span = ExpectedSpan(field_name="name", span_offset=(5, 8), source_text="홍길동")
        item = _make_item(spans=[span])
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
            actual_spans=[("name", (5, 8))],
        )
        assert result.metrics.span_accuracy == 1.0

    def test_span_iou_partial(self):
        span = ExpectedSpan(field_name="name", span_offset=(0, 10), source_text="홍길동")
        item = _make_item(spans=[span])
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
            actual_spans=[("name", (5, 15))],  # 부분 겹침
        )
        assert 0.0 <= result.metrics.span_accuracy <= 1.0

    def test_overall_score_weighted(self):
        item = _make_item()
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
        )
        m = result.metrics
        expected = (0.40 * m.field_accuracy + 0.20 * m.span_accuracy
                    + 0.25 * m.required_field_coverage + 0.15 * m.type_correctness)
        assert abs(m.overall_score - expected) < 1e-6

    def test_empty_golden_item(self):
        item = _make_item(fields=[])
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동"},
        )
        assert result.metrics.field_accuracy == 1.0  # nothing to evaluate

    def test_evaluate_extraction_set(self):
        item = _make_item()
        gset = GoldenExtractionSet(
            name="Test Set",
            document_type="POLICY",
            created_by="tester",
            items=[item, item],
        )
        results = self.evaluator.evaluate_extraction_set(
            golden_set=gset,
            actual_results=[
                {"name": "홍길동", "amount": 500},
                {"name": "이순신", "amount": 100},
            ],
        )
        assert len(results) == 2

    def test_field_detail_populated(self):
        item = _make_item()
        result = self.evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
        )
        assert len(result.field_details) == 2
        for d in result.field_details:
            assert d.field_name in ("name", "amount")


# ---------------------------------------------------------------------------
# ExtractionMetrics 모델 검증
# ---------------------------------------------------------------------------

class TestExtractionMetrics:
    def test_valid_metrics(self):
        m = ExtractionMetrics(
            field_accuracy=0.9,
            span_accuracy=0.85,
            required_field_coverage=0.95,
            type_correctness=0.92,
            overall_score=0.90,
        )
        assert m.field_accuracy == 0.9

    def test_out_of_range_raises(self):
        with pytest.raises(Exception):
            ExtractionMetrics(
                field_accuracy=1.5,
                span_accuracy=0.85,
                required_field_coverage=0.95,
                type_correctness=0.92,
                overall_score=0.90,
            )


# ---------------------------------------------------------------------------
# GoldenExtractionItem 모델 검증
# ---------------------------------------------------------------------------

class TestGoldenExtractionItem:
    def test_valid_item(self):
        item = _make_item()
        assert len(item.expected_fields) == 2

    def test_expected_span_invalid_offset(self):
        with pytest.raises(Exception):
            ExpectedSpan(
                field_name="name",
                span_offset=(10, 5),  # start > end
                source_text="x",
            )


# ---------------------------------------------------------------------------
# QualityGateChecker
# ---------------------------------------------------------------------------

class TestQualityGateChecker:
    def setup_method(self):
        self.checker = QualityGateChecker()

    def test_all_pass(self):
        result = _make_eval_result(
            field_accuracy=0.95, span_accuracy=0.90,
            required_field_coverage=0.98, type_correctness=0.95,
            overall_score=0.94,
        )
        gate = self.checker.check(result)
        assert gate.passed is True
        assert gate.failures == []

    def test_field_accuracy_fail(self):
        result = _make_eval_result(field_accuracy=0.70, overall_score=0.70)
        gate = self.checker.check(result, field_accuracy_threshold=0.80)
        assert gate.passed is False
        assert any("field_accuracy" in f for f in gate.failures)

    def test_overall_score_fail(self):
        result = _make_eval_result(overall_score=0.50)
        gate = self.checker.check(result, overall_score_threshold=0.85)
        assert gate.passed is False

    def test_multiple_failures(self):
        result = _make_eval_result(
            field_accuracy=0.50, span_accuracy=0.50,
            required_field_coverage=0.50, type_correctness=0.50,
            overall_score=0.50,
        )
        gate = self.checker.check(result)
        assert not gate.passed
        assert len(gate.failures) >= 2


# ---------------------------------------------------------------------------
# EvaluationReportGenerator
# ---------------------------------------------------------------------------

class TestEvaluationReportGenerator:
    def test_generate_report_not_empty(self):
        gen = EvaluationReportGenerator()
        item = _make_item()
        evaluator = ExtractionEvaluator()
        result = evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
        )
        report = gen.generate_markdown(result)
        assert len(report) > 0
        assert "추출 품질 평가" in report

    def test_report_contains_metrics(self):
        gen = EvaluationReportGenerator()
        item = _make_item()
        evaluator = ExtractionEvaluator()
        result = evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields={"name": "홍길동", "amount": 500},
        )
        report = gen.generate_markdown(result)
        assert "Field Accuracy" in report or "field_accuracy" in report
