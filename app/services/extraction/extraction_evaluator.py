"""
ExtractionEvaluator + EvaluationReportGenerator — Phase 8 FG8.3 (task8-10).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.models.extraction_evaluation import (
    ExtractionEvaluationResult,
    ExtractionMetrics,
    FieldEvaluationDetail,
    GoldenExtractionItem,
    GoldenExtractionSet,
    QualityGateResult,
)
from app.services.extraction.diff_calculator import (
    DiffCalculator,
    SpanBasedDiffCalculator,
    _levenshtein_similarity,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 품질 점수 가중치
_WEIGHTS = {
    "field_accuracy": 0.40,
    "span_accuracy": 0.20,
    "required_field_coverage": 0.25,
    "type_correctness": 0.15,
}


def _check_type(expected_type: str, value: Any) -> bool:
    mapping = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "float": float,
        "boolean": bool,
        "array": list,
        "object": dict,
        "date": str,
    }
    t = mapping.get(expected_type)
    if t is None:
        return True
    return isinstance(value, t)


class ExtractionEvaluator:
    """단일 GoldenExtractionItem 또는 배치를 평가한다."""

    def __init__(
        self,
        diff_calc: Optional[DiffCalculator] = None,
        span_calc: Optional[SpanBasedDiffCalculator] = None,
    ):
        self._diff = diff_calc or DiffCalculator(fuzzy_threshold=0.8)
        self._span = span_calc or SpanBasedDiffCalculator()

    def evaluate_extraction(
        self,
        golden_item: GoldenExtractionItem,
        actual_fields: Dict[str, Any],
        actual_spans: Optional[List[Tuple[str, Tuple[int, int]]]] = None,
        extraction_candidate_id: Optional[UUID] = None,
        actor_type: str = "user",
        scope_profile_id: Optional[UUID] = None,
    ) -> ExtractionEvaluationResult:
        """단일 golden item에 대해 평가를 수행한다."""
        field_details: List[FieldEvaluationDetail] = []

        expected_dict = {f.field_name: f for f in golden_item.expected_fields}
        actual_spans_by_field: Dict[str, List[Tuple[int, int]]] = {}
        if actual_spans:
            for fname, offset in actual_spans:
                actual_spans_by_field.setdefault(fname, []).append(offset)

        # 기대 span 인덱스
        expected_spans_by_field: Dict[str, List[Tuple[int, int]]] = {}
        for es in golden_item.expected_spans:
            expected_spans_by_field.setdefault(es.field_name, []).append(es.span_offset)

        for field_name, expected_field in expected_dict.items():
            actual_value = actual_fields.get(field_name)
            expected_value = expected_field.expected_value

            is_exact = actual_value == expected_value
            if isinstance(expected_value, str) and isinstance(actual_value, str):
                fuzzy_sim = _levenshtein_similarity(expected_value, actual_value)
            elif actual_value is None or expected_value is None:
                fuzzy_sim = 1.0 if actual_value == expected_value else 0.0
            else:
                fuzzy_sim = 1.0 if is_exact else 0.0

            type_ok = _check_type(expected_field.field_type, actual_value) if actual_value is not None else False

            # span IoU
            span_iou_val: Optional[float] = None
            if field_name in expected_spans_by_field:
                exp_offsets = expected_spans_by_field[field_name]
                act_offsets = actual_spans_by_field.get(field_name, [])
                span_iou_val = self._span.compare_spans(exp_offsets, act_offsets)

            field_details.append(FieldEvaluationDetail(
                field_name=field_name,
                expected_value=expected_value,
                actual_value=actual_value,
                is_exact_match=is_exact,
                fuzzy_similarity=fuzzy_sim,
                type_correct=type_ok,
                is_required=expected_field.required,
                span_iou=span_iou_val,
            ))

        metrics = self._compute_metrics(field_details)

        return ExtractionEvaluationResult(
            golden_set_id=golden_item.golden_set_id,
            golden_item_id=golden_item.id,
            extraction_candidate_id=extraction_candidate_id,
            metrics=metrics,
            field_details=field_details,
            evaluated_at=utcnow(),
            actor_type=actor_type,
            scope_profile_id=scope_profile_id,
        )

    def evaluate_extraction_set(
        self,
        golden_set: GoldenExtractionSet,
        actual_results: List[Dict[str, Any]],
        actor_type: str = "user",
    ) -> List[ExtractionEvaluationResult]:
        """배치 평가: golden_set의 item 목록과 actual_results를 1:1 매핑한다."""
        results = []
        for item, actual in zip(golden_set.items, actual_results):
            result = self.evaluate_extraction(
                golden_item=item,
                actual_fields=actual,
                actor_type=actor_type,
            )
            results.append(result)
        return results

    def _compute_metrics(self, details: List[FieldEvaluationDetail]) -> ExtractionMetrics:
        if not details:
            return ExtractionMetrics(
                field_accuracy=1.0,
                span_accuracy=1.0,
                required_field_coverage=1.0,
                type_correctness=1.0,
                overall_score=1.0,
            )

        n = len(details)

        field_accuracy = sum(1 for d in details if d.is_exact_match) / n

        spans_with_iou = [d for d in details if d.span_iou is not None]
        span_accuracy = (
            sum(d.span_iou for d in spans_with_iou) / len(spans_with_iou)
            if spans_with_iou else 1.0
        )

        required = [d for d in details if d.is_required]
        required_field_coverage = (
            sum(1 for d in required if d.actual_value is not None) / len(required)
            if required else 1.0
        )

        type_correctness = sum(1 for d in details if d.type_correct) / n

        overall_score = (
            _WEIGHTS["field_accuracy"] * field_accuracy
            + _WEIGHTS["span_accuracy"] * span_accuracy
            + _WEIGHTS["required_field_coverage"] * required_field_coverage
            + _WEIGHTS["type_correctness"] * type_correctness
        )

        return ExtractionMetrics(
            field_accuracy=field_accuracy,
            span_accuracy=span_accuracy,
            required_field_coverage=required_field_coverage,
            type_correctness=type_correctness,
            overall_score=overall_score,
        )


_REPORT_TEMPLATE = """\
# 추출 품질 평가 보고서

**평가 일시**: {{ evaluated_at }}
**전체 점수**: {{ "%.4f" | format(metrics.overall_score) }}

## 요약 지표

| 지표 | 점수 |
|------|------|
| Field Accuracy | {{ "%.4f" | format(metrics.field_accuracy) }} |
| Span Accuracy | {{ "%.4f" | format(metrics.span_accuracy) }} |
| Required Field Coverage | {{ "%.4f" | format(metrics.required_field_coverage) }} |
| Type Correctness | {{ "%.4f" | format(metrics.type_correctness) }} |
| **Overall Score** | **{{ "%.4f" | format(metrics.overall_score) }}** |

## 필드별 상세

{% for d in field_details %}
- **{{ d.field_name }}**: exact={{ d.is_exact_match }}, similarity={{ "%.3f" | format(d.fuzzy_similarity) }}, type_ok={{ d.type_correct }}{% if d.span_iou is not none %}, span_iou={{ "%.3f" | format(d.span_iou) }}{% endif %}
{% endfor %}
"""


class EvaluationReportGenerator:
    """Jinja2 기반 마크다운 평가 보고서를 생성한다."""

    def __init__(self):
        try:
            from jinja2 import Environment, select_autoescape
            self._env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
            self._template = self._env.from_string(_REPORT_TEMPLATE)
            self._available = True
        except ImportError:
            self._available = False
            logger.warning("jinja2 not installed — report generation disabled")

    def generate_markdown(self, result: ExtractionEvaluationResult) -> str:
        if not self._available:
            return self._fallback_report(result)

        return self._template.render(
            evaluated_at=result.evaluated_at.isoformat() if result.evaluated_at else "N/A",
            metrics=result.metrics,
            field_details=result.field_details,
        )

    def _fallback_report(self, result: ExtractionEvaluationResult) -> str:
        m = result.metrics
        lines = [
            "# 추출 품질 평가 보고서",
            f"overall_score={m.overall_score:.4f}",
            f"field_accuracy={m.field_accuracy:.4f}",
            f"span_accuracy={m.span_accuracy:.4f}",
            f"required_field_coverage={m.required_field_coverage:.4f}",
            f"type_correctness={m.type_correctness:.4f}",
        ]
        return "\n".join(lines)


class QualityGateChecker:
    """품질 임계값 통과 여부를 판단한다."""

    def check(
        self,
        result: ExtractionEvaluationResult,
        field_accuracy_threshold: float = 0.80,
        span_accuracy_threshold: float = 0.80,
        required_field_coverage_threshold: float = 0.95,
        type_correctness_threshold: float = 0.90,
        overall_score_threshold: float = 0.85,
    ) -> QualityGateResult:
        m = result.metrics
        failures = []

        if m.field_accuracy < field_accuracy_threshold:
            failures.append(
                f"field_accuracy {m.field_accuracy:.4f} < {field_accuracy_threshold}"
            )
        if m.span_accuracy < span_accuracy_threshold:
            failures.append(
                f"span_accuracy {m.span_accuracy:.4f} < {span_accuracy_threshold}"
            )
        if m.required_field_coverage < required_field_coverage_threshold:
            failures.append(
                f"required_field_coverage {m.required_field_coverage:.4f} < {required_field_coverage_threshold}"
            )
        if m.type_correctness < type_correctness_threshold:
            failures.append(
                f"type_correctness {m.type_correctness:.4f} < {type_correctness_threshold}"
            )
        if m.overall_score < overall_score_threshold:
            failures.append(
                f"overall_score {m.overall_score:.4f} < {overall_score_threshold}"
            )

        return QualityGateResult(
            passed=len(failures) == 0,
            field_accuracy=m.field_accuracy,
            span_accuracy=m.span_accuracy,
            required_field_coverage=m.required_field_coverage,
            type_correctness=m.type_correctness,
            overall_score=m.overall_score,
            failures=failures,
        )
