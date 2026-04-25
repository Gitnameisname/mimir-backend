"""
ApprovedExtraction 도메인 모델 — Phase 8 FG8.2.

사용자(또는 에이전트)가 ExtractionCandidate를 승인·수정한 결과를 영구 저장한다.
Document.metadata와 분리하여 독립 artifact로 관리 (S1 원칙 ②).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from app.utils.converters import uuid_str_or_none


class HumanEdit(BaseModel):
    """인간이 수정한 필드 단건 기록."""

    field_name: str
    before_value: Any
    after_value: Any
    edited_at: datetime
    edited_by: str
    reason: Optional[str] = None


class ApprovedExtraction(BaseModel):
    """최종 승인된 추출 결과 artifact."""

    id: UUID
    candidate_id: Optional[UUID] = None

    document_id: UUID
    document_version: int
    extraction_schema_id: str
    extraction_schema_version: int

    extraction_model: str
    extraction_latency_ms: int
    extraction_tokens: Optional[Dict[str, int]] = None
    extraction_cost_estimate: Optional[float] = None
    extraction_prompt_version: Optional[str] = None

    approved_fields: Dict[str, Any] = Field(default_factory=dict)
    human_edits: List[HumanEdit] = Field(default_factory=list)

    approved_by: str
    approved_at: datetime
    approval_comment: Optional[str] = None

    actor_type: str = "user"

    scope_profile_id: Optional[UUID] = None

    created_at: datetime
    updated_at: datetime

    is_soft_deleted: bool = False

    @model_validator(mode="after")
    def _validate_approved_fields(self) -> "ApprovedExtraction":
        if not self.approved_fields:
            raise ValueError("approved_fields는 최소 1개 이상의 필드를 포함해야 함")
        return self


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------

class ApproveExtractionRequest(BaseModel):
    """POST /{id}/approve"""
    approval_comment: Optional[str] = Field(default=None, max_length=1024)


class ModifyExtractionRequest(BaseModel):
    """POST /{id}/modify — 일부 필드 수정 후 승인."""
    modifications: Dict[str, Any] = Field(
        ..., description="수정할 필드: {field_name: new_value}"
    )
    reasons: Optional[Dict[str, str]] = Field(
        default=None, description="수정 이유: {field_name: reason}"
    )
    approval_comment: Optional[str] = Field(default=None, max_length=1024)


class RejectExtractionRequest(BaseModel):
    """POST /{id}/reject"""
    reason: str = Field(..., min_length=1, max_length=1024)


class BatchApproveRequest(BaseModel):
    """POST /batch-approve"""
    extraction_ids: List[UUID] = Field(..., min_length=1, max_length=100)
    approval_comment: Optional[str] = Field(default=None, max_length=1024)


class BatchRejectRequest(BaseModel):
    """POST /batch-reject"""
    extraction_ids: List[UUID] = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# Response DTO
# ---------------------------------------------------------------------------

class ApprovedExtractionResponse(BaseModel):
    """ApprovedExtraction 응답."""

    id: str
    candidate_id: Optional[str]
    document_id: str
    document_version: int
    extraction_schema_id: str
    extraction_schema_version: int
    extraction_model: str
    approved_fields: Dict[str, Any]
    human_edits: List[Dict[str, Any]]
    approved_by: str
    approved_at: str
    approval_comment: Optional[str]
    actor_type: str
    scope_profile_id: Optional[str]
    created_at: str

    @classmethod
    def from_domain(cls, ae: ApprovedExtraction) -> "ApprovedExtractionResponse":
        return cls(
            id=str(ae.id),
            candidate_id=uuid_str_or_none(ae.candidate_id),
            document_id=str(ae.document_id),
            document_version=ae.document_version,
            extraction_schema_id=ae.extraction_schema_id,
            extraction_schema_version=ae.extraction_schema_version,
            extraction_model=ae.extraction_model,
            approved_fields=ae.approved_fields,
            human_edits=[e.model_dump(mode="json") for e in ae.human_edits],
            approved_by=ae.approved_by,
            approved_at=ae.approved_at.isoformat(),
            approval_comment=ae.approval_comment,
            actor_type=ae.actor_type,
            scope_profile_id=uuid_str_or_none(ae.scope_profile_id),
            created_at=ae.created_at.isoformat(),
        )
