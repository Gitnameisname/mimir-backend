"""
추출 품질 평가 모델 — Phase 8 FG8.3 (task8-10).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ExpectedField(BaseModel):
    field_name: str
    expected_value: Any
    field_type: str = "string"
    required: bool = True
    description: Optional[str] = None


class ExpectedSpan(BaseModel):
    field_name: str
    span_offset: Tuple[int, int]
    source_text: str
    node_id: Optional[str] = None

    @field_validator("span_offset")
    @classmethod
    def _validate_offset(cls, v):
        if len(v) != 2 or v[0] >= v[1]:
            raise ValueError("span_offset must be (start, end) where start < end")
        return v


class GoldenExtractionItem(BaseModel):
    """추출 품질 평가를 위한 기준 데이터."""

    id: Optional[UUID] = None
    golden_set_id: Optional[UUID] = None
    document_id: UUID
    document_version: int = 1
    document_type: str
    expected_fields: List[ExpectedField] = Field(default_factory=list)
    expected_spans: List[ExpectedSpan] = Field(default_factory=list)
    created_by: str = "system"
    created_at: Optional[datetime] = None


class GoldenExtractionSet(BaseModel):
    """GoldenExtractionItem 컬렉션."""

    id: Optional[UUID] = None
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    document_type: str
    version: int = 1
    created_by: str
    scope_profile_id: Optional[UUID] = None
    actor_type: str = "user"
    items: List[GoldenExtractionItem] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FieldEvaluationDetail(BaseModel):
    field_name: str
    expected_value: Any
    actual_value: Any
    is_exact_match: bool
    fuzzy_similarity: float = Field(ge=0.0, le=1.0)
    type_correct: bool
    is_required: bool
    span_iou: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ExtractionMetrics(BaseModel):
    field_accuracy: float = Field(ge=0.0, le=1.0)
    span_accuracy: float = Field(ge=0.0, le=1.0)
    required_field_coverage: float = Field(ge=0.0, le=1.0)
    type_correctness: float = Field(ge=0.0, le=1.0)
    overall_score: float = Field(ge=0.0, le=1.0)


class ExtractionEvaluationResult(BaseModel):
    id: Optional[UUID] = None
    golden_set_id: Optional[UUID] = None
    golden_item_id: Optional[UUID] = None
    extraction_candidate_id: Optional[UUID] = None
    metrics: ExtractionMetrics
    field_details: List[FieldEvaluationDetail] = Field(default_factory=list)
    evaluated_at: Optional[datetime] = None
    evaluated_by: str = "system"
    actor_type: str = "user"
    scope_profile_id: Optional[UUID] = None


class QualityGateResult(BaseModel):
    """CI 품질 게이트 통과 여부."""

    passed: bool
    field_accuracy: float
    span_accuracy: float
    required_field_coverage: float
    type_correctness: float
    overall_score: float
    failures: List[str] = Field(default_factory=list)


class RunEvaluationRequest(BaseModel):
    golden_set_id: UUID
    extraction_candidate_ids: Optional[List[UUID]] = None
    scope_profile_id: Optional[UUID] = None


class QualityGateCheckRequest(BaseModel):
    evaluation_id: UUID
    field_accuracy_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    span_accuracy_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    required_field_coverage_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    type_correctness_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    overall_score_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
