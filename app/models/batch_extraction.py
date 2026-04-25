"""
배치 재추출 작업 도메인 모델 — Phase 8 Task 8-7.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from app.utils.converters import uuid_str_or_none


class BatchJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchExtractionJob(BaseModel):
    id: UUID
    extraction_schema_id: str
    extraction_schema_version: int
    scope_profile_id: Optional[UUID] = None

    status: BatchJobStatus = BatchJobStatus.PENDING
    total_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    progress_percentage: float = 0.0

    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    sample_count: Optional[int] = None
    sample_mode: bool = False
    comparison_mode: bool = False
    comparison_report_path: Optional[str] = None

    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_completion_at: Optional[datetime] = None

    current_processing: Optional[int] = None
    error_summary: Optional[str] = Field(default=None, max_length=2000)
    failed_document_ids: List[str] = Field(default_factory=list)

    created_by: str
    is_cancellation_requested: bool = False
    actor_type: str = "user"

    @field_validator("completed_count", "failed_count", "skipped_count")
    @classmethod
    def non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Count must be non-negative")
        return v

    @field_validator("progress_percentage")
    @classmethod
    def valid_progress(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("Progress must be between 0.0 and 100.0")
        return v


class ExtractionRetryLog(BaseModel):
    id: UUID
    job_id: UUID
    document_id: UUID
    attempt_number: int
    status: str  # "success" | "failed" | "skipped"
    error_reason: Optional[str] = None
    latency_ms: int
    created_at: datetime


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------

class BatchRetryRequest(BaseModel):
    extraction_schema_id: str = Field(..., min_length=1, max_length=100)
    extraction_schema_version: int = Field(default=1, ge=1)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    sample_count: Optional[int] = Field(default=None, ge=1, le=10000)
    comparison_mode: bool = False

    @model_validator(mode="after")
    def _validate_date_range(self) -> "BatchRetryRequest":
        if self.date_from and self.date_to:
            if self.date_from > self.date_to:
                raise ValueError("date_from must be before date_to")
            from datetime import timedelta
            if (self.date_to - self.date_from) > timedelta(days=365):
                raise ValueError("date range must not exceed 365 days")
        return self


class SampleRetryRequest(BaseModel):
    extraction_schema_id: str = Field(..., min_length=1, max_length=100)
    extraction_schema_version: int = Field(default=1, ge=1)
    sample_count: int = Field(default=10, ge=1, le=100)


class CancelBatchRequest(BaseModel):
    reason: Optional[str] = None


class BatchExtractionJobResponse(BaseModel):
    id: str
    extraction_schema_id: str
    extraction_schema_version: int
    status: BatchJobStatus
    total_count: int
    completed_count: int
    failed_count: int
    skipped_count: int
    progress_percentage: float
    sample_mode: bool
    comparison_mode: bool
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    estimated_completion_at: Optional[str] = None
    error_summary: Optional[str] = None
    failed_document_ids: List[str]
    scope_profile_id: Optional[str] = None
    created_by: str
    actor_type: str

    @classmethod
    def from_domain(cls, job: BatchExtractionJob) -> "BatchExtractionJobResponse":
        def _dt(v: Optional[datetime]) -> Optional[str]:
            return v.isoformat() if v else None

        return cls(
            id=str(job.id),
            extraction_schema_id=job.extraction_schema_id,
            extraction_schema_version=job.extraction_schema_version,
            status=job.status,
            total_count=job.total_count,
            completed_count=job.completed_count,
            failed_count=job.failed_count,
            skipped_count=job.skipped_count,
            progress_percentage=job.progress_percentage,
            sample_mode=job.sample_mode,
            comparison_mode=job.comparison_mode,
            created_at=job.created_at.isoformat(),
            started_at=_dt(job.started_at),
            completed_at=_dt(job.completed_at),
            estimated_completion_at=_dt(job.estimated_completion_at),
            error_summary=job.error_summary,
            failed_document_ids=job.failed_document_ids,
            scope_profile_id=uuid_str_or_none(job.scope_profile_id),
            created_by=job.created_by,
            actor_type=job.actor_type,
        )
