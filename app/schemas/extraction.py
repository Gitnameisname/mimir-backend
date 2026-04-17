"""
Extraction Schema API 요청/응답 Pydantic 스키마 — Phase 8 FG8.1

CreateExtractionSchemaRequest : 추출 스키마 생성 요청
UpdateExtractionSchemaRequest : 추출 스키마 업데이트 요청
ExtractionSchemaResponse      : 스키마 단건 응답
ExtractionSchemaVersionResponse: 버전 이력 응답
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.extraction import ExtractionFieldDef


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class CreateExtractionSchemaRequest(BaseModel):
    """POST /extraction-schemas 요청 바디."""

    doc_type_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="DocumentType.type_code (예: 'POLICY')",
    )
    fields: Dict[str, ExtractionFieldDef] = Field(
        ...,
        description="필드 정의 맵. 키는 필드명(snake_case), 값은 ExtractionFieldDef",
    )
    scope_profile_id: Optional[str] = Field(
        default=None,
        description="Scope Profile ID (UUID 문자열, optional)",
    )
    extra_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="추가 메타데이터",
    )


class UpdateExtractionSchemaRequest(BaseModel):
    """PUT /extraction-schemas/{doc_type} 요청 바디."""

    fields: Dict[str, ExtractionFieldDef] = Field(
        ...,
        description="업데이트된 필드 정의 (전체 교체)",
    )
    change_summary: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="변경 요약 (버전 이력에 기록)",
    )


class DeprecateExtractionSchemaRequest(BaseModel):
    """PATCH /extraction-schemas/{doc_type}/deprecate 요청 바디."""

    reason: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="폐기 사유",
    )


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class ExtractionSchemaResponse(BaseModel):
    """추출 스키마 단건 응답."""

    id: str
    doc_type_code: str
    version: int
    fields: Dict[str, Any]
    is_deprecated: bool
    deprecation_reason: Optional[str]
    created_at: str
    updated_at: str
    created_by: str
    updated_by: str
    scope_profile_id: Optional[str]
    extra_metadata: Dict[str, Any]

    @classmethod
    def from_domain(cls, schema) -> "ExtractionSchemaResponse":
        return cls(
            id=str(schema.id),
            doc_type_code=schema.doc_type_code,
            version=schema.version,
            fields={k: v.model_dump() for k, v in schema.fields.items()},
            is_deprecated=schema.is_deprecated,
            deprecation_reason=schema.deprecation_reason,
            created_at=schema.created_at.isoformat(),
            updated_at=schema.updated_at.isoformat(),
            created_by=schema.created_by,
            updated_by=schema.updated_by,
            scope_profile_id=str(schema.scope_profile_id) if schema.scope_profile_id else None,
            extra_metadata=schema.extra_metadata,
        )


class ExtractionSchemaVersionResponse(BaseModel):
    """버전 이력 단건 응답."""

    id: str
    schema_id: str
    version: int
    fields: Dict[str, Any]
    is_deprecated: bool
    deprecation_reason: Optional[str]
    change_summary: Optional[str]
    changed_fields: List[str]
    created_at: str
    created_by: str

    @classmethod
    def from_domain(cls, v) -> "ExtractionSchemaVersionResponse":
        return cls(
            id=str(v.id),
            schema_id=str(v.schema_id),
            version=v.version,
            fields={k: fd.model_dump() for k, fd in v.fields.items()},
            is_deprecated=v.is_deprecated,
            deprecation_reason=v.deprecation_reason,
            change_summary=v.change_summary,
            changed_fields=v.changed_fields,
            created_at=v.created_at.isoformat(),
            created_by=v.created_by,
        )
