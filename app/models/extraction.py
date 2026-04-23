"""
Extraction 도메인 모델 (Pydantic v2 기반) — Phase 8 FG8.1

ExtractionFieldDef  : 개별 필드 추출 정의 (타입, 제약, 지침)
ExtractionTargetSchema : DocumentType별 추출 스키마 (필드 집합 + 버전)

S2 원칙 준수:
  ① DocumentType 하드코딩 금지 — doc_type_code 참조만 저장
  ⑤ actor_type 필드 감사 로그 포함
  ⑥ Scope Profile ACL 슬롯 (scope_profile_id)
  ⑦ 폐쇄망 동등성 — 외부 의존 없음
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# ExtractionFieldDef
# ---------------------------------------------------------------------------

FieldType = Literal["string", "number", "date", "boolean", "array", "object", "enum"]


class ExtractionFieldDef(BaseModel):
    """추출 대상 필드 단위 정의."""

    field_name: str = Field(..., min_length=1, max_length=255)
    field_type: FieldType
    required: bool = False
    description: str = Field(..., min_length=1, max_length=1024)

    # 검증 규칙
    pattern: Optional[str] = None
    instruction: Optional[str] = Field(default=None, max_length=2048)
    examples: List[str] = Field(default_factory=list)

    # 크기/범위 제약
    max_length: Optional[int] = Field(default=None, ge=1, le=65536)
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    # 타입별 추가 속성
    date_format: Optional[str] = None
    enum_values: Optional[List[str]] = None
    default_value: Optional[Any] = None

    # object 타입 중첩 스키마
    nested_schema: Optional[Dict[str, "ExtractionFieldDef"]] = None

    model_config = {"json_schema_extra": {}}

    @field_validator("field_name")
    @classmethod
    def _validate_snake_case(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError("필드명은 snake_case 형식이어야 함 (소문자, 숫자, 언더스코어)")
        return v

    @field_validator("pattern", mode="before")
    @classmethod
    def _validate_pattern(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"정규식 오류: {e}")
        return v

    @model_validator(mode="after")
    def _validate_type_consistency(self) -> "ExtractionFieldDef":
        ft = self.field_type

        if ft == "string":
            if self.enum_values is not None:
                raise ValueError("string 타입에서는 enum_values를 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("string 타입에서는 min_value/max_value를 사용할 수 없음")

        elif ft == "number":
            if self.pattern is not None:
                raise ValueError("number 타입에서는 pattern을 사용할 수 없음")
            if self.max_length is not None:
                raise ValueError("number 타입에서는 max_length를 사용할 수 없음")
            if self.enum_values is not None:
                raise ValueError("number 타입에서는 enum_values를 사용할 수 없음")
            if self.min_value is not None and self.max_value is not None:
                if self.min_value > self.max_value:
                    raise ValueError("min_value는 max_value보다 작거나 같아야 함")

        elif ft == "date":
            if self.pattern is not None:
                raise ValueError("date 타입에서는 pattern을 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("date 타입에서는 min_value/max_value를 사용할 수 없음")
            if self.enum_values is not None:
                raise ValueError("date 타입에서는 enum_values를 사용할 수 없음")

        elif ft == "boolean":
            if self.pattern is not None:
                raise ValueError("boolean 타입에서는 pattern을 사용할 수 없음")
            if self.max_length is not None:
                raise ValueError("boolean 타입에서는 max_length를 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("boolean 타입에서는 min_value/max_value를 사용할 수 없음")
            if self.enum_values is not None:
                raise ValueError("boolean 타입에서는 enum_values를 사용할 수 없음")

        elif ft == "enum":
            if not self.enum_values:
                raise ValueError("enum 타입에서는 enum_values가 필수임")
            if self.pattern is not None:
                raise ValueError("enum 타입에서는 pattern을 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("enum 타입에서는 min_value/max_value를 사용할 수 없음")

        elif ft == "array":
            if self.pattern is not None:
                raise ValueError("array 타입에서는 pattern을 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("array 타입에서는 min_value/max_value를 사용할 수 없음")
            if self.enum_values is not None:
                raise ValueError("array 타입에서는 enum_values를 사용할 수 없음")

        elif ft == "object":
            if not self.nested_schema:
                raise ValueError("object 타입에서는 nested_schema가 필수임")
            if self.pattern is not None:
                raise ValueError("object 타입에서는 pattern을 사용할 수 없음")
            if self.max_length is not None:
                raise ValueError("object 타입에서는 max_length를 사용할 수 없음")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("object 타입에서는 min_value/max_value를 사용할 수 없음")

        return self

    @model_validator(mode="after")
    def _validate_examples(self) -> "ExtractionFieldDef":
        if not self.examples:
            return self

        ft = self.field_type

        if ft == "number":
            for ex in self.examples:
                try:
                    float(ex)
                except (ValueError, TypeError):
                    raise ValueError(f"number 타입의 예제 '{ex}'가 숫자로 변환 불가")

        elif ft == "enum" and self.enum_values:
            for ex in self.examples:
                if ex not in self.enum_values:
                    raise ValueError(
                        f"enum 타입의 예제 '{ex}'가 enum_values에 없음"
                    )

        elif ft == "date":
            fmt = self.date_format or "YYYY-MM-DD"
            py_fmt = fmt.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
            for ex in self.examples:
                try:
                    datetime.strptime(ex, py_fmt)
                except ValueError:
                    raise ValueError(
                        f"date 타입의 예제 '{ex}'가 형식 '{fmt}'과 맞지 않음"
                    )

        return self

    @model_validator(mode="after")
    def _validate_default_value(self) -> "ExtractionFieldDef":
        """default_value 가 field_type 과 호환되는지 검증.

        - None 은 항상 허용 (기본값 미지정).
        - bool 은 Python 에서 int 의 서브클래스이므로 number 검증에서 제외.
        - array 타입에 원소별 타입 검증은 하지 않음(일반 리스트만 확인).
        - object 타입은 dict 여부만 확인(nested_schema 재귀 검증은 별도 범위).
        """
        if self.default_value is None:
            return self

        ft = self.field_type
        dv = self.default_value

        if ft == "string":
            if not isinstance(dv, str):
                raise ValueError(
                    f"string 타입의 default_value 는 문자열이어야 함 (입력 타입: {type(dv).__name__})"
                )

        elif ft == "number":
            # bool 은 int 의 서브클래스지만 number 타입과 호환되지 않음.
            if isinstance(dv, bool) or not isinstance(dv, (int, float)):
                raise ValueError(
                    f"number 타입의 default_value 는 숫자여야 함 (입력 타입: {type(dv).__name__})"
                )
            # min_value/max_value 범위 검증.
            if self.min_value is not None and float(dv) < self.min_value:
                raise ValueError(
                    f"number 타입의 default_value({dv}) 가 min_value({self.min_value}) 보다 작음"
                )
            if self.max_value is not None and float(dv) > self.max_value:
                raise ValueError(
                    f"number 타입의 default_value({dv}) 가 max_value({self.max_value}) 보다 큼"
                )

        elif ft == "boolean":
            if not isinstance(dv, bool):
                raise ValueError(
                    f"boolean 타입의 default_value 는 bool 이어야 함 (입력 타입: {type(dv).__name__})"
                )

        elif ft == "date":
            if not isinstance(dv, str):
                raise ValueError(
                    f"date 타입의 default_value 는 형식에 맞는 문자열이어야 함 (입력 타입: {type(dv).__name__})"
                )
            fmt = self.date_format or "YYYY-MM-DD"
            py_fmt = fmt.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
            try:
                datetime.strptime(dv, py_fmt)
            except ValueError:
                raise ValueError(
                    f"date 타입의 default_value '{dv}' 가 형식 '{fmt}' 과 맞지 않음"
                )

        elif ft == "enum":
            if not isinstance(dv, str):
                raise ValueError(
                    f"enum 타입의 default_value 는 문자열이어야 함 (입력 타입: {type(dv).__name__})"
                )
            if self.enum_values and dv not in self.enum_values:
                raise ValueError(
                    f"enum 타입의 default_value '{dv}' 가 enum_values 에 없음"
                )

        elif ft == "array":
            if not isinstance(dv, list):
                raise ValueError(
                    f"array 타입의 default_value 는 리스트여야 함 (입력 타입: {type(dv).__name__})"
                )

        elif ft == "object":
            if not isinstance(dv, dict):
                raise ValueError(
                    f"object 타입의 default_value 는 객체(dict)여야 함 (입력 타입: {type(dv).__name__})"
                )

        return self


# ---------------------------------------------------------------------------
# ExtractionTargetSchema
# ---------------------------------------------------------------------------

class ExtractionTargetSchema(BaseModel):
    """DocumentType별 추출 대상 스키마."""

    id: UUID
    doc_type_code: str = Field(..., description="DocumentType.type_code 참조")
    version: int = Field(default=1, ge=1)

    fields: Dict[str, ExtractionFieldDef] = Field(default_factory=dict)

    is_deprecated: bool = False
    deprecation_reason: Optional[str] = Field(default=None, max_length=1024)

    # 감사 정보
    created_at: datetime
    updated_at: datetime
    created_by: str = Field(..., description="actor_id (user 또는 agent)")
    updated_by: str = Field(..., description="actor_id (user 또는 agent)")

    # Scope Profile ACL 슬롯
    scope_profile_id: Optional[UUID] = None

    extra_metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"json_schema_extra": {}}

    @model_validator(mode="after")
    def _validate_deprecation(self) -> "ExtractionTargetSchema":
        if self.is_deprecated and not self.deprecation_reason:
            raise ValueError("is_deprecated=True일 때 deprecation_reason이 필수")
        if not self.is_deprecated and self.deprecation_reason:
            raise ValueError("is_deprecated=False일 때 deprecation_reason을 설정할 수 없음")
        return self

    @model_validator(mode="after")
    def _validate_fields_not_empty(self) -> "ExtractionTargetSchema":
        if not self.fields:
            raise ValueError("추출 스키마는 최소 1개의 필드를 포함해야 함")
        return self


# ---------------------------------------------------------------------------
# ExtractionSchemaVersion (버전 이력용 읽기 전용 뷰)
# ---------------------------------------------------------------------------

class ExtractionSchemaVersion(BaseModel):
    """버전 이력 단일 레코드 (읽기 전용)."""

    id: UUID
    schema_id: UUID
    version: int
    fields: Dict[str, ExtractionFieldDef]
    is_deprecated: bool = False
    deprecation_reason: Optional[str] = None
    change_summary: Optional[str] = None
    changed_fields: List[str] = Field(default_factory=list)
    created_at: datetime
    created_by: str
    extra_metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# FG8.2: ExtractionCandidate 도메인 모델
# ---------------------------------------------------------------------------

from enum import Enum


class ExtractionStatus(str, Enum):
    """추출 캔디데이트 상태."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


class ExtractionMode(str, Enum):
    """추출 모드."""
    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"


class ExtractionConfidenceScore(BaseModel):
    """필드별 신뢰도 점수."""
    field_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: Optional[str] = None


class HumanEditRecord(BaseModel):
    """인간이 수정한 필드 기록."""
    field_name: str
    before_value: Any
    after_value: Any
    edited_at: datetime
    edited_by: str
    reason: Optional[str] = None


class ExtractionCandidate(BaseModel):
    """
    추출 캔디데이트 도메인 모델.

    LLM 자동 추출 결과를 캡슐화한다.
    status=pending → 사용자 승인 대기 큐 진입.
    """

    id: UUID
    document_id: UUID
    document_version: int
    extraction_schema_id: str = Field(..., description="ExtractionTargetSchema.doc_type_code")
    extraction_schema_version: int

    extracted_fields: Dict[str, Any] = Field(default_factory=dict)
    confidence_scores: List[ExtractionConfidenceScore] = Field(default_factory=list)

    extraction_model: str
    extraction_mode: ExtractionMode = ExtractionMode.DETERMINISTIC
    extraction_latency_ms: int
    extraction_tokens: Optional[Dict[str, int]] = None
    extraction_cost_estimate: Optional[float] = None
    extraction_prompt_version: Optional[str] = None
    document_content_hash: Optional[str] = None

    status: ExtractionStatus = ExtractionStatus.PENDING

    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    human_feedback: Optional[str] = None
    human_edits: List[HumanEditRecord] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime

    actor_type: str = "agent"

    scope_profile_id: Optional[UUID] = None

    is_soft_deleted: bool = False
