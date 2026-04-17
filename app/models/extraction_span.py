"""
SourceSpan 역참조 모델 — Phase 8 FG8.3 (task8-8).

추출된 데이터가 원문의 정확한 위치를 추적할 수 있도록 한다.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class SourceSpan(BaseModel):
    """원문 내 단일 스팬(span) 위치 정보."""

    id: Optional[UUID] = None
    document_id: UUID
    version_id: Optional[UUID] = None
    node_id: Optional[UUID] = None

    # 문자(character) 오프셋 기반 — UTF-8 문자 단위 (바이트 아님)
    span_offset: Tuple[int, int] = Field(..., description="(start_char, end_char) 0-based 반개구간")
    source_text: str = Field(..., description="해당 오프셋의 실제 텍스트")
    content_hash: Optional[str] = Field(default=None, description="SHA-256 of source_text")

    created_at: Optional[datetime] = None

    @field_validator("span_offset")
    @classmethod
    def _validate_offset(cls, v: Tuple[int, int]) -> Tuple[int, int]:
        if len(v) != 2:
            raise ValueError("span_offset must be (start, end) tuple")
        start, end = v
        if start < 0:
            raise ValueError("span start must be >= 0")
        if start >= end:
            raise ValueError("span start must be < end")
        return v

    @model_validator(mode="after")
    def _ensure_hash(self) -> "SourceSpan":
        if not self.content_hash and self.source_text:
            self.content_hash = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        return self

    def verify_against_document(self, document_text: str) -> bool:
        """문서 전체 텍스트에서 해당 오프셋의 텍스트가 source_text와 일치하는지 검증한다."""
        start, end = self.span_offset
        if end > len(document_text):
            return False
        actual = document_text[start:end]
        return actual == self.source_text

    @property
    def length(self) -> int:
        return self.span_offset[1] - self.span_offset[0]


class ExtractedFieldWithAttribution(BaseModel):
    """추출 필드 + 원문 위치(attribution) 정보."""

    field_name: str
    extracted_value: Any
    source_spans: List[SourceSpan] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # 다중 span 병합 여부 (overlapping 스팬이 자동 병합된 경우)
    spans_merged: bool = False

    def has_attribution(self) -> bool:
        return len(self.source_spans) > 0


class ExtractionResultWithAttribution(BaseModel):
    """추출 결과 전체 + 필드별 attribution."""

    extraction_candidate_id: UUID
    document_id: UUID
    fields: List[ExtractedFieldWithAttribution] = Field(default_factory=list)
    total_span_count: int = 0

    @model_validator(mode="after")
    def _compute_total(self) -> "ExtractionResultWithAttribution":
        self.total_span_count = sum(len(f.source_spans) for f in self.fields)
        return self

    def get_field(self, field_name: str) -> Optional[ExtractedFieldWithAttribution]:
        for f in self.fields:
            if f.field_name == field_name:
                return f
        return None


class SpanHighlight(BaseModel):
    """UI 하이라이트용 경량 표현."""

    start: int
    end: int
    field_name: str
    source_text: str
    confidence: Optional[float] = None
