"""
ExtractionRecord + 재현성 검증 모델 — Phase 8 FG8.3 (task8-9).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MatchStatus(str, Enum):
    IDENTICAL = "identical"
    PARTIAL = "partial"
    MISMATCH = "mismatch"


class DiffDetail(BaseModel):
    """단일 필드 차이점 상세."""

    field_name: str
    original_value: Any
    new_value: Any
    match_type: str  # "exact", "fuzzy", "type_mismatch", "missing"
    similarity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    span_iou: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class VerificationResult(BaseModel):
    """재추출 검증 결과."""

    id: Optional[UUID] = None
    extraction_candidate_id: UUID
    verified_at: datetime
    match_status: MatchStatus
    field_match_count: int = 0
    field_total_count: int = 0
    field_accuracy: float = Field(default=0.0, ge=0.0, le=1.0)
    diff_details: List[DiffDetail] = Field(default_factory=list)
    error_message: Optional[str] = None
    verified_by: str = "system"
    actor_type: str = "user"


class ExtractionRecord(BaseModel):
    """
    추출 작업의 전체 조건과 결과를 불변 기록으로 저장한다.

    동일 조건 재실행 시 동일 결과가 나와야 함 (deterministic 모드).
    """

    id: Optional[UUID] = None
    extraction_candidate_id: UUID
    document_id: UUID
    document_version: int = 1
    document_content_hash: Optional[str] = None

    extraction_schema_id: str
    extraction_schema_version: int

    extraction_model: str
    model_version: Optional[str] = None
    extraction_prompt_version: Optional[str] = None
    extraction_mode: str = "deterministic"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: Optional[int] = None

    extracted_result: Dict[str, Any] = Field(default_factory=dict)
    extracted_timestamp: Optional[datetime] = None

    scope_profile_id: Optional[UUID] = None
    actor_type: str = "agent"
    created_at: Optional[datetime] = None


class ExtractionRecordResponse(BaseModel):
    id: UUID
    extraction_candidate_id: UUID
    document_id: UUID
    document_version: int
    document_content_hash: Optional[str]
    extraction_schema_id: str
    extraction_schema_version: int
    extraction_model: str
    extraction_mode: str
    temperature: float
    extracted_result: Dict[str, Any]
    extracted_timestamp: Optional[datetime]
    created_at: Optional[datetime]

    @classmethod
    def from_domain(cls, record: ExtractionRecord) -> "ExtractionRecordResponse":
        return cls(
            id=record.id,
            extraction_candidate_id=record.extraction_candidate_id,
            document_id=record.document_id,
            document_version=record.document_version,
            document_content_hash=record.document_content_hash,
            extraction_schema_id=record.extraction_schema_id,
            extraction_schema_version=record.extraction_schema_version,
            extraction_model=record.extraction_model,
            extraction_mode=record.extraction_mode,
            temperature=record.temperature,
            extracted_result=record.extracted_result,
            extracted_timestamp=record.extracted_timestamp,
            created_at=record.created_at,
        )


class VerifyExtractionRequest(BaseModel):
    """재검증 요청."""
    override_temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    override_seed: Optional[int] = None
    fields_to_verify: Optional[List[str]] = None


class VerificationResultResponse(BaseModel):
    id: Optional[UUID]
    extraction_candidate_id: UUID
    verified_at: datetime
    match_status: str
    field_accuracy: float
    diff_details: List[DiffDetail]
    error_message: Optional[str]

    @classmethod
    def from_domain(cls, vr: VerificationResult) -> "VerificationResultResponse":
        return cls(
            id=vr.id,
            extraction_candidate_id=vr.extraction_candidate_id,
            verified_at=vr.verified_at,
            match_status=vr.match_status.value,
            field_accuracy=vr.field_accuracy,
            diff_details=vr.diff_details,
            error_message=vr.error_message,
        )
