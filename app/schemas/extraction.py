"""
Extraction Schema API 요청/응답 Pydantic 스키마 — Phase 8 FG8.1

CreateExtractionSchemaRequest : 추출 스키마 생성 요청
UpdateExtractionSchemaRequest : 추출 스키마 업데이트 요청
ExtractionSchemaResponse      : 스키마 단건 응답
ExtractionSchemaVersionResponse: 버전 이력 응답
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.extraction import ExtractionFieldDef
from app.utils.converters import uuid_str_or_none


# ---------------------------------------------------------------------------
# 공용 검증 유틸 (P2-C / P3-C 보강)
# ---------------------------------------------------------------------------

# doc_type_code 형식: 영문자로 시작, 영문/숫자/하이픈/언더스코어. 프론트와 동일.
_DOC_TYPE_CODE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")

# 사용자 자유 텍스트에 허용하지 않는 C0 제어문자 (Tab, LF, CR 제외).
_FORBIDDEN_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

# P3-C: DoS 내성 상한.
#   * 최상위 fields 최대 개수
#   * nested_schema 재귀 최대 깊이 (depth=0: 최상위 필드, depth=1: nested 직계, ...)
MAX_FIELDS_COUNT = 200
MAX_NESTED_DEPTH = 3


def _strip_and_validate_text(value: Optional[str], *, field_label: str) -> Optional[str]:
    """앞뒤 공백 제거 후 제어문자 차단. None 이면 그대로 반환."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        # min_length 가드는 이미 Field 제약으로 처리되지만, 공백만 있는 문자열을 빈 값으로 수렴.
        raise ValueError(f"{field_label}은(는) 공백만으로 구성될 수 없음")
    if _FORBIDDEN_CONTROL_RE.search(stripped):
        raise ValueError(f"{field_label}에 제어문자가 포함됨")
    return stripped


def _max_nested_depth(field: ExtractionFieldDef, *, _cur: int = 0) -> int:
    """ExtractionFieldDef 와 그 nested_schema 를 재귀적으로 내려가며 최대 깊이 반환.

    depth=0 이 자기 자신(최상위). nested_schema 가 있으면 그 자식들의 최대 깊이 + 1.
    순환 참조가 발생할 구조는 아니지만(Pydantic 모델은 생성 시점 이후 불변 트리) ,
    안전장치로 MAX_NESTED_DEPTH+2 를 넘으면 즉시 반환한다.
    """
    if _cur > MAX_NESTED_DEPTH + 2:
        return _cur
    nested = getattr(field, "nested_schema", None)
    if not nested:
        return _cur
    best = _cur
    for child in nested.values():
        d = _max_nested_depth(child, _cur=_cur + 1)
        if d > best:
            best = d
    return best


def _validate_fields_map(
    v: Dict[str, ExtractionFieldDef],
) -> Dict[str, ExtractionFieldDef]:
    """fields 공통 검증: 비어있지 않고 개수/깊이 상한을 만족."""
    if not v:
        raise ValueError("fields 는 최소 1개 이상의 필드가 필요함")
    if len(v) > MAX_FIELDS_COUNT:
        raise ValueError(
            f"fields 최상위 필드 개수는 {MAX_FIELDS_COUNT}개 이하여야 함 (입력: {len(v)})"
        )
    for name, fd in v.items():
        depth = _max_nested_depth(fd)
        if depth > MAX_NESTED_DEPTH:
            raise ValueError(
                f"fields['{name}'] 의 nested_schema 깊이가 허용치를 초과함 "
                f"(입력 깊이: {depth}, 최대: {MAX_NESTED_DEPTH})"
            )
    return v


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

    @field_validator("doc_type_code")
    @classmethod
    def _validate_doc_type_code(cls, v: str) -> str:
        # P7-1-b: document_types.type_code 는 케이스-센시티브 PK 이고 시드
        # 및 /admin/document-types CreateDocTypeModal 이 모두 대문자 로 저장하므로,
        # 여기서도 대문자로 정규화해 FK 참조가 깨지는 실수를 근본 차단.
        # 클라이언트가 'contract' 를 보내든 'Contract' 를 보내든 서버는 'CONTRACT'
        # 로 일관되게 기록하고, 기존 대문자 규약과 호환된다.
        v = v.strip().upper()
        if not _DOC_TYPE_CODE_RE.match(v):
            raise ValueError(
                "doc_type_code 는 영문자로 시작하고 영문/숫자/하이픈/언더스코어만 허용됨"
            )
        return v

    @field_validator("scope_profile_id")
    @classmethod
    def _validate_scope_profile_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        try:
            # 단순히 파싱만으로 UUID 형식 검증 (버전 무관).
            UUID(v)
        except (ValueError, TypeError):
            raise ValueError("scope_profile_id 는 UUID 형식이어야 함")
        return v

    @field_validator("fields")
    @classmethod
    def _validate_fields(
        cls, v: Dict[str, ExtractionFieldDef]
    ) -> Dict[str, ExtractionFieldDef]:
        return _validate_fields_map(v)


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

    @field_validator("fields")
    @classmethod
    def _validate_fields(
        cls, v: Dict[str, ExtractionFieldDef]
    ) -> Dict[str, ExtractionFieldDef]:
        return _validate_fields_map(v)

    @field_validator("change_summary")
    @classmethod
    def _validate_change_summary(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        # 빈 문자열은 None 으로 수렴 (프론트 trim 후 빈 값일 수 있음)
        if v.strip() == "":
            return None
        return _strip_and_validate_text(v, field_label="change_summary")


class DeprecateExtractionSchemaRequest(BaseModel):
    """PATCH /extraction-schemas/{doc_type}/deprecate 요청 바디."""

    reason: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="폐기 사유",
    )

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, v: str) -> str:
        cleaned = _strip_and_validate_text(v, field_label="reason")
        # mypy: _strip_and_validate_text 는 None 입력에만 None 반환. v 는 필수 str.
        assert cleaned is not None
        return cleaned


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
            scope_profile_id=uuid_str_or_none(schema.scope_profile_id),
            extra_metadata=schema.extra_metadata,
        )


class ExtractionSchemaVersionResponse(BaseModel):
    """버전 이력 단건 응답.

    P5-2: `rolled_back_from_version` 필드를 `extra_metadata` 에서 뽑아 1급 필드로
    노출한다. 프론트 "반복 rollback 경고" 가 `change_summary` 파싱에 의존하지
    않도록 계약을 명시화하는 목적. `extra_metadata` 자체는 노출하지 않아
    내부 메타데이터 누설을 피한다.
    """

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
    # P5-2: 이 버전이 롤백으로 생성되었을 때, 어느 과거 버전을 기준으로 복원되었는지.
    # 일반 편집(update) / 최초 생성(create) 에서는 None.
    rolled_back_from_version: Optional[int] = None

    @classmethod
    def from_domain(cls, v) -> "ExtractionSchemaVersionResponse":
        meta = getattr(v, "extra_metadata", None) or {}
        raw = meta.get("rolled_back_from_version") if isinstance(meta, dict) else None
        # P5-2 보안: 외부에서 주입된 이상한 값(문자열, 리스트 등)으로 프론트가
        # 혼란을 일으키지 않도록 엄격하게 int 로만 허용. bool 은 int 의 서브타입
        # 이므로 명시적으로 제외.
        rolled_back_from: Optional[int]
        if isinstance(raw, bool):
            rolled_back_from = None
        elif isinstance(raw, int) and raw >= 1:
            rolled_back_from = raw
        else:
            rolled_back_from = None

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
            rolled_back_from_version=rolled_back_from,
        )


# ---------------------------------------------------------------------------
# P4-A/B: 버전 Diff / Rollback
# ---------------------------------------------------------------------------


class PropertyDiff(BaseModel):
    """수정된 필드 내 개별 속성 변화."""

    key: str
    before: Any
    after: Any


class ModifiedFieldDiff(BaseModel):
    """fields 맵에서 양쪽에 모두 존재하지만 속성 값이 다른 필드."""

    name: str
    changes: List[PropertyDiff]


class ExtractionSchemaDiffResponse(BaseModel):
    """버전 간 fields 차이 응답 (P4-A).

    base → target 방향으로 집계. added 는 target 에만, removed 는 base 에만.
    """

    doc_type_code: str
    base_version: int
    target_version: int
    added: List[str]
    removed: List[str]
    modified: List[ModifiedFieldDiff]
    unchanged_count: int


class RollbackExtractionSchemaRequest(BaseModel):
    """POST /extraction-schemas/{doc_type}/rollback 요청 (P4-B).

    target_version 의 fields 를 새 버전(current+1) 으로 복사.
    change_summary 가 비어 있으면 자동 요약이 기록된다.
    """

    target_version: int = Field(..., ge=1, description="되돌릴 대상 버전 번호")
    change_summary: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="롤백 사유/메모 (선택)",
    )
    scope_profile_id: Optional[str] = Field(
        default=None,
        description="Scope Profile ID (UUID 문자열, optional). 지정 시 해당 scope 의 스키마에만 적용.",
    )

    @field_validator("change_summary")
    @classmethod
    def _validate_change_summary(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v.strip() == "":
            return None
        return _strip_and_validate_text(v, field_label="change_summary")

    @field_validator("scope_profile_id")
    @classmethod
    def _validate_scope_profile_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        try:
            UUID(v)
        except (ValueError, TypeError):
            raise ValueError("scope_profile_id 는 UUID 형식이어야 함")
        return v


# ---------------------------------------------------------------------------
# Diff 계산 유틸 (서버 측 정본)
# ---------------------------------------------------------------------------


def _deep_equal(a: Any, b: Any) -> bool:
    """dict/list 재귀 동치. JSON 직렬화 가능 값에 국한."""
    if a is b:
        return True
    if type(a) is not type(b):
        # bool 과 int 혼동 방지: Python 에서 True == 1 이지만 타입이 다르면 false.
        return False
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def compute_fields_diff(
    base: Dict[str, Any],
    target: Dict[str, Any],
    *,
    doc_type_code: str,
    base_version: int,
    target_version: int,
) -> ExtractionSchemaDiffResponse:
    """두 fields 맵의 차이를 계산해 DTO 로 반환.

    프론트(P2-B) 의 computeFieldsDiff 와 동일한 의미론:
      - added: target 에만 있는 키
      - removed: base 에만 있는 키
      - modified: 양쪽에 있고 내용이 다른 키 + 속성별 변화 리스트
      - unchanged_count: 양쪽에 있고 내용 동일한 키 수
    """
    base_keys = set(base.keys())
    target_keys = set(target.keys())
    added = sorted(target_keys - base_keys)
    removed = sorted(base_keys - target_keys)
    modified: List[ModifiedFieldDiff] = []
    unchanged = 0

    for k in sorted(base_keys & target_keys):
        b = base[k] or {}
        t = target[k] or {}
        if _deep_equal(b, t):
            unchanged += 1
            continue
        prop_keys = sorted(set(b.keys()) | set(t.keys()))
        prop_changes: List[PropertyDiff] = []
        for pk in prop_keys:
            bv = b.get(pk)
            tv = t.get(pk)
            if not _deep_equal(bv, tv):
                prop_changes.append(PropertyDiff(key=pk, before=bv, after=tv))
        if prop_changes:
            modified.append(ModifiedFieldDiff(name=k, changes=prop_changes))

    return ExtractionSchemaDiffResponse(
        doc_type_code=doc_type_code,
        base_version=base_version,
        target_version=target_version,
        added=added,
        removed=removed,
        modified=modified,
        unchanged_count=unchanged,
    )
