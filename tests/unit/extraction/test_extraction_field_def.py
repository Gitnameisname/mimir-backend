"""
ExtractionFieldDef / ExtractionTargetSchema 단위 테스트 — Phase 8 FG8.1

테스트 범위:
- 각 field_type별 정상 생성
- 타입 일관성 검증 (type consistency violations)
- 필드 제약조건 검증 (pattern, enum_values, min/max 등)
- 정규식 오류 처리
- 예제 데이터 검증
- ExtractionTargetSchema deprecated / empty fields 검증
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.extraction import ExtractionFieldDef, ExtractionTargetSchema


_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------

def _string_field(**kwargs) -> ExtractionFieldDef:
    defaults = dict(
        field_name="field_one",
        field_type="string",
        required=True,
        description="문자열 필드",
        examples=["abc", "def"],
    )
    defaults.update(kwargs)
    return ExtractionFieldDef(**defaults)


def _schema_with(**field_kwargs) -> ExtractionTargetSchema:
    return ExtractionTargetSchema(
        id=uuid4(),
        doc_type_code="POLICY",
        version=1,
        fields={"f": _string_field(**field_kwargs)},
        created_at=_NOW,
        updated_at=_NOW,
        created_by="user_001",
        updated_by="user_001",
    )


# ---------------------------------------------------------------------------
# TestExtractionFieldDef — 정상 생성
# ---------------------------------------------------------------------------

class TestExtractionFieldDefCreation:
    def test_string_field(self):
        f = ExtractionFieldDef(
            field_name="invoice_number",
            field_type="string",
            required=True,
            description="인보이스 번호",
            pattern=r"^INV-\d{6}$",
            examples=["INV-000001", "INV-999999"],
            max_length=20,
        )
        assert f.field_name == "invoice_number"
        assert f.field_type == "string"
        assert f.required is True
        assert f.max_length == 20

    def test_number_field(self):
        f = ExtractionFieldDef(
            field_name="total_amount",
            field_type="number",
            required=True,
            description="총액",
            examples=["1000.50", "2500.00"],
            min_value=0.0,
            max_value=999999.99,
        )
        assert f.field_type == "number"
        assert f.min_value == 0.0
        assert f.max_value == 999999.99

    def test_date_field(self):
        f = ExtractionFieldDef(
            field_name="issue_date",
            field_type="date",
            required=True,
            description="발행일",
            date_format="YYYY-MM-DD",
            examples=["2026-04-17", "2026-01-01"],
        )
        assert f.field_type == "date"
        assert f.date_format == "YYYY-MM-DD"

    def test_boolean_field(self):
        f = ExtractionFieldDef(
            field_name="is_approved",
            field_type="boolean",
            required=False,
            description="승인 여부",
            examples=["true", "false"],
        )
        assert f.field_type == "boolean"

    def test_enum_field(self):
        f = ExtractionFieldDef(
            field_name="status",
            field_type="enum",
            required=True,
            description="상태",
            enum_values=["draft", "approved", "rejected"],
            examples=["draft", "approved"],
        )
        assert f.field_type == "enum"
        assert len(f.enum_values) == 3

    def test_array_field(self):
        f = ExtractionFieldDef(
            field_name="tags",
            field_type="array",
            required=False,
            description="태그 목록",
            examples=["['a', 'b']", "['c']"],
        )
        assert f.field_type == "array"

    def test_object_field_with_nested_schema(self):
        f = ExtractionFieldDef(
            field_name="buyer_info",
            field_type="object",
            required=True,
            description="구매자 정보",
            nested_schema={
                "buyer_name": ExtractionFieldDef(
                    field_name="buyer_name",
                    field_type="string",
                    required=True,
                    description="이름",
                    examples=["홍길동", "김철수"],
                ),
                "buyer_email": ExtractionFieldDef(
                    field_name="buyer_email",
                    field_type="string",
                    required=False,
                    description="이메일",
                    examples=["a@b.com", "c@d.com"],
                ),
            },
            examples=["{}"],
        )
        assert f.field_type == "object"
        assert "buyer_name" in f.nested_schema
        assert "buyer_email" in f.nested_schema

    def test_field_with_instruction(self):
        f = ExtractionFieldDef(
            field_name="clause_number",
            field_type="string",
            required=True,
            description="조항 번호",
            instruction="원문의 조항 번호를 정확히 추출. 없으면 null.",
            examples=["1.2", "3.4.5"],
        )
        assert f.instruction is not None

    def test_field_with_default_value(self):
        f = ExtractionFieldDef(
            field_name="currency",
            field_type="string",
            required=False,
            description="통화",
            default_value="KRW",
            examples=["KRW", "USD"],
        )
        assert f.default_value == "KRW"


# ---------------------------------------------------------------------------
# TestExtractionFieldDef — 필드명 검증
# ---------------------------------------------------------------------------

class TestFieldNameValidation:
    def test_valid_snake_case(self):
        f = ExtractionFieldDef(
            field_name="my_field_123",
            field_type="string",
            required=False,
            description="필드",
            examples=["a", "b"],
        )
        assert f.field_name == "my_field_123"

    def test_invalid_pascal_case(self):
        with pytest.raises(ValidationError, match="snake_case"):
            ExtractionFieldDef(
                field_name="InvoiceNumber",
                field_type="string",
                required=False,
                description="필드",
                examples=["a"],
            )

    def test_invalid_starts_with_number(self):
        with pytest.raises(ValidationError, match="snake_case"):
            ExtractionFieldDef(
                field_name="1field",
                field_type="string",
                required=False,
                description="필드",
                examples=["a"],
            )

    def test_invalid_has_hyphen(self):
        with pytest.raises(ValidationError, match="snake_case"):
            ExtractionFieldDef(
                field_name="my-field",
                field_type="string",
                required=False,
                description="필드",
                examples=["a"],
            )


# ---------------------------------------------------------------------------
# TestExtractionFieldDef — 정규식 패턴 검증
# ---------------------------------------------------------------------------

class TestPatternValidation:
    def test_valid_pattern(self):
        f = ExtractionFieldDef(
            field_name="code",
            field_type="string",
            required=True,
            description="코드",
            pattern=r"^[A-Z]{3}-\d{4}$",
            examples=["ABC-1234", "XYZ-9999"],
        )
        assert f.pattern is not None

    def test_invalid_regex_pattern(self):
        with pytest.raises(ValidationError, match="정규식 오류"):
            ExtractionFieldDef(
                field_name="code",
                field_type="string",
                required=True,
                description="코드",
                pattern=r"[invalid(regex",
                examples=["a"],
            )


# ---------------------------------------------------------------------------
# TestExtractionFieldDef — 타입 일관성 검증
# ---------------------------------------------------------------------------

class TestTypeConsistency:
    def test_string_with_enum_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values"):
            ExtractionFieldDef(
                field_name="type_code",
                field_type="string",
                required=True,
                description="타입",
                enum_values=["A", "B"],
                examples=["A"],
            )

    def test_string_with_min_value_raises(self):
        with pytest.raises(ValidationError, match="min_value"):
            ExtractionFieldDef(
                field_name="name",
                field_type="string",
                required=True,
                description="이름",
                min_value=0.0,
                examples=["a"],
            )

    def test_number_with_pattern_raises(self):
        with pytest.raises(ValidationError, match="pattern"):
            ExtractionFieldDef(
                field_name="code",
                field_type="number",
                required=True,
                description="코드",
                pattern=r"\d+",
                examples=["123"],
            )

    def test_number_with_max_length_raises(self):
        with pytest.raises(ValidationError, match="max_length"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=True,
                description="금액",
                max_length=10,
                examples=["100"],
            )

    def test_number_min_gt_max_raises(self):
        with pytest.raises(ValidationError, match="min_value"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=True,
                description="금액",
                min_value=1000.0,
                max_value=100.0,
                examples=["500"],
            )

    def test_date_with_pattern_raises(self):
        with pytest.raises(ValidationError, match="pattern"):
            ExtractionFieldDef(
                field_name="date_field",
                field_type="date",
                required=True,
                description="날짜",
                pattern=r"\d{4}-\d{2}-\d{2}",
                examples=["2026-01-01"],
            )

    def test_boolean_with_enum_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values"):
            ExtractionFieldDef(
                field_name="flag",
                field_type="boolean",
                required=False,
                description="플래그",
                enum_values=["true", "false"],
                examples=["true"],
            )

    def test_enum_without_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values가 필수"):
            ExtractionFieldDef(
                field_name="status",
                field_type="enum",
                required=True,
                description="상태",
                enum_values=None,
                examples=["a"],
            )

    def test_enum_with_empty_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values가 필수"):
            ExtractionFieldDef(
                field_name="status",
                field_type="enum",
                required=True,
                description="상태",
                enum_values=[],
                examples=["a"],
            )

    def test_object_without_nested_schema_raises(self):
        with pytest.raises(ValidationError, match="nested_schema가 필수"):
            ExtractionFieldDef(
                field_name="info",
                field_type="object",
                required=True,
                description="정보",
                nested_schema=None,
                examples=["{}"],
            )

    def test_array_with_enum_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values"):
            ExtractionFieldDef(
                field_name="items",
                field_type="array",
                required=False,
                description="항목",
                enum_values=["a", "b"],
                examples=["['a']"],
            )


# ---------------------------------------------------------------------------
# TestExtractionFieldDef — 예제 검증
# ---------------------------------------------------------------------------

class TestExampleValidation:
    def test_number_example_non_numeric_raises(self):
        with pytest.raises(ValidationError, match="숫자로 변환 불가"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=True,
                description="금액",
                examples=["not_a_number"],
            )

    def test_enum_example_not_in_values_raises(self):
        with pytest.raises(ValidationError, match="enum_values에 없음"):
            ExtractionFieldDef(
                field_name="status",
                field_type="enum",
                required=True,
                description="상태",
                enum_values=["approved", "rejected"],
                examples=["approved", "invalid_status"],
            )

    def test_date_example_wrong_format_raises(self):
        with pytest.raises(ValidationError, match="형식.*맞지 않음"):
            ExtractionFieldDef(
                field_name="issue_date",
                field_type="date",
                required=True,
                description="발행일",
                date_format="YYYY-MM-DD",
                examples=["17/04/2026"],  # wrong format
            )

    def test_valid_date_examples_pass(self):
        f = ExtractionFieldDef(
            field_name="issue_date",
            field_type="date",
            required=True,
            description="발행일",
            date_format="YYYY-MM-DD",
            examples=["2026-01-01", "2026-12-31"],
        )
        assert len(f.examples) == 2


# ---------------------------------------------------------------------------
# TestExtractionTargetSchema
# ---------------------------------------------------------------------------

class TestExtractionTargetSchema:
    def test_schema_creation(self):
        schema = ExtractionTargetSchema(
            id=uuid4(),
            doc_type_code="POLICY",
            version=1,
            fields={
                "clause_number": ExtractionFieldDef(
                    field_name="clause_number",
                    field_type="string",
                    required=True,
                    description="조항 번호",
                    examples=["1.2", "3.4"],
                ),
                "effective_date": ExtractionFieldDef(
                    field_name="effective_date",
                    field_type="date",
                    required=False,
                    description="발효일",
                    date_format="YYYY-MM-DD",
                    examples=["2026-01-01", "2026-06-30"],
                ),
            },
            created_at=_NOW,
            updated_at=_NOW,
            created_by="user_001",
            updated_by="user_001",
        )
        assert schema.version == 1
        assert len(schema.fields) == 2
        assert schema.doc_type_code == "POLICY"

    def test_deprecated_without_reason_raises(self):
        with pytest.raises(ValidationError, match="deprecation_reason이 필수"):
            ExtractionTargetSchema(
                id=uuid4(),
                doc_type_code="POLICY",
                version=1,
                fields={
                    "f": ExtractionFieldDef(
                        field_name="f",
                        field_type="string",
                        required=True,
                        description="필드",
                        examples=["a", "b"],
                    )
                },
                is_deprecated=True,
                deprecation_reason=None,
                created_at=_NOW,
                updated_at=_NOW,
                created_by="user_001",
                updated_by="user_001",
            )

    def test_not_deprecated_with_reason_raises(self):
        with pytest.raises(ValidationError, match="deprecation_reason을 설정할 수 없음"):
            ExtractionTargetSchema(
                id=uuid4(),
                doc_type_code="POLICY",
                version=1,
                fields={
                    "f": ExtractionFieldDef(
                        field_name="f",
                        field_type="string",
                        required=True,
                        description="필드",
                        examples=["a", "b"],
                    )
                },
                is_deprecated=False,
                deprecation_reason="이유 없이 설정",
                created_at=_NOW,
                updated_at=_NOW,
                created_by="user_001",
                updated_by="user_001",
            )

    def test_empty_fields_raises(self):
        with pytest.raises(ValidationError, match="최소 1개의 필드"):
            ExtractionTargetSchema(
                id=uuid4(),
                doc_type_code="POLICY",
                version=1,
                fields={},
                created_at=_NOW,
                updated_at=_NOW,
                created_by="user_001",
                updated_by="user_001",
            )

    def test_deprecated_with_reason_valid(self):
        schema = ExtractionTargetSchema(
            id=uuid4(),
            doc_type_code="MANUAL",
            version=2,
            fields={
                "title": ExtractionFieldDef(
                    field_name="title",
                    field_type="string",
                    required=True,
                    description="제목",
                    examples=["제목1", "제목2"],
                )
            },
            is_deprecated=True,
            deprecation_reason="새 스키마로 대체됨",
            created_at=_NOW,
            updated_at=_NOW,
            created_by="agent_007",
            updated_by="agent_007",
        )
        assert schema.is_deprecated is True
        assert schema.deprecation_reason == "새 스키마로 대체됨"

    def test_schema_with_scope_profile(self):
        sp_id = uuid4()
        schema = ExtractionTargetSchema(
            id=uuid4(),
            doc_type_code="REPORT",
            version=1,
            fields={
                "report_title": ExtractionFieldDef(
                    field_name="report_title",
                    field_type="string",
                    required=True,
                    description="보고서 제목",
                    examples=["월간보고서", "분기보고서"],
                )
            },
            scope_profile_id=sp_id,
            created_at=_NOW,
            updated_at=_NOW,
            created_by="user_002",
            updated_by="user_002",
        )
        assert schema.scope_profile_id == sp_id

    def test_json_round_trip(self):
        schema = ExtractionTargetSchema(
            id=uuid4(),
            doc_type_code="POLICY",
            version=1,
            fields={
                "responsible_dept": ExtractionFieldDef(
                    field_name="responsible_dept",
                    field_type="string",
                    required=True,
                    description="담당 부서",
                    examples=["기술팀", "운영팀"],
                )
            },
            created_at=_NOW,
            updated_at=_NOW,
            created_by="user_001",
            updated_by="user_001",
        )
        json_str = schema.model_dump_json()
        restored = ExtractionTargetSchema.model_validate_json(json_str)
        assert restored.doc_type_code == schema.doc_type_code
        assert "responsible_dept" in restored.fields


# ---------------------------------------------------------------------------
# TestDefaultValueCompatibility — P3 후속-A
#   default_value ↔ field_type 호환성 검증 (서버측 Pydantic 계층).
# ---------------------------------------------------------------------------

class TestDefaultValueCompatibility:
    """default_value 가 field_type 과 호환되지 않으면 거절되어야 함.

    프론트 DefaultValueWidget 이 UI 경고를 띄우지만 우회 가능 (JSON 모드, API 직접 호출).
    서버에서 model_validator 로 방어한다.
    """

    # --- none allowed across all types ------------------------------------

    def test_none_is_always_allowed(self):
        for ft in ("string", "number", "boolean", "date", "array"):
            f = ExtractionFieldDef(
                field_name="f",
                field_type=ft,
                required=False,
                description="설명",
                default_value=None,
            )
            assert f.default_value is None

    # --- string -----------------------------------------------------------

    def test_string_accepts_string_default(self):
        f = ExtractionFieldDef(
            field_name="currency",
            field_type="string",
            required=False,
            description="통화",
            default_value="KRW",
        )
        assert f.default_value == "KRW"

    def test_string_rejects_int_default(self):
        with pytest.raises(ValidationError, match="문자열이어야"):
            ExtractionFieldDef(
                field_name="currency",
                field_type="string",
                required=False,
                description="통화",
                default_value=1234,
            )

    def test_string_rejects_bool_default(self):
        with pytest.raises(ValidationError, match="문자열이어야"):
            ExtractionFieldDef(
                field_name="currency",
                field_type="string",
                required=False,
                description="통화",
                default_value=True,
            )

    # --- number -----------------------------------------------------------

    def test_number_accepts_int(self):
        f = ExtractionFieldDef(
            field_name="amount",
            field_type="number",
            required=False,
            description="금액",
            default_value=42,
        )
        assert f.default_value == 42

    def test_number_accepts_float(self):
        f = ExtractionFieldDef(
            field_name="ratio",
            field_type="number",
            required=False,
            description="비율",
            default_value=3.14,
        )
        assert f.default_value == 3.14

    def test_number_rejects_string(self):
        with pytest.raises(ValidationError, match="숫자여야"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=False,
                description="금액",
                default_value="42",
            )

    def test_number_rejects_bool(self):
        # bool 은 int 의 서브클래스지만 number 로는 거절해야 함.
        with pytest.raises(ValidationError, match="숫자여야"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=False,
                description="금액",
                default_value=True,
            )

    def test_number_default_below_min_rejected(self):
        with pytest.raises(ValidationError, match="min_value"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=False,
                description="금액",
                min_value=0.0,
                max_value=100.0,
                default_value=-1,
            )

    def test_number_default_above_max_rejected(self):
        with pytest.raises(ValidationError, match="max_value"):
            ExtractionFieldDef(
                field_name="amount",
                field_type="number",
                required=False,
                description="금액",
                min_value=0.0,
                max_value=100.0,
                default_value=999,
            )

    # --- boolean ----------------------------------------------------------

    def test_boolean_accepts_true(self):
        f = ExtractionFieldDef(
            field_name="is_active",
            field_type="boolean",
            required=False,
            description="활성 여부",
            default_value=True,
        )
        assert f.default_value is True

    def test_boolean_accepts_false(self):
        f = ExtractionFieldDef(
            field_name="is_active",
            field_type="boolean",
            required=False,
            description="활성 여부",
            default_value=False,
        )
        assert f.default_value is False

    def test_boolean_rejects_int(self):
        with pytest.raises(ValidationError, match="bool"):
            ExtractionFieldDef(
                field_name="is_active",
                field_type="boolean",
                required=False,
                description="활성 여부",
                default_value=1,
            )

    def test_boolean_rejects_string(self):
        with pytest.raises(ValidationError, match="bool"):
            ExtractionFieldDef(
                field_name="is_active",
                field_type="boolean",
                required=False,
                description="활성 여부",
                default_value="true",
            )

    # --- date -------------------------------------------------------------

    def test_date_accepts_matching_format(self):
        f = ExtractionFieldDef(
            field_name="signed_on",
            field_type="date",
            required=False,
            description="서명일",
            date_format="YYYY-MM-DD",
            default_value="2026-04-21",
        )
        assert f.default_value == "2026-04-21"

    def test_date_rejects_non_string(self):
        with pytest.raises(ValidationError, match="문자열이어야"):
            ExtractionFieldDef(
                field_name="signed_on",
                field_type="date",
                required=False,
                description="서명일",
                date_format="YYYY-MM-DD",
                default_value=20260421,
            )

    def test_date_rejects_wrong_format(self):
        with pytest.raises(ValidationError, match="형식"):
            ExtractionFieldDef(
                field_name="signed_on",
                field_type="date",
                required=False,
                description="서명일",
                date_format="YYYY-MM-DD",
                default_value="04/21/2026",
            )

    # --- enum -------------------------------------------------------------

    def test_enum_accepts_member(self):
        f = ExtractionFieldDef(
            field_name="status",
            field_type="enum",
            required=False,
            description="상태",
            enum_values=["draft", "active", "archived"],
            default_value="active",
        )
        assert f.default_value == "active"

    def test_enum_rejects_non_member(self):
        with pytest.raises(ValidationError, match="enum_values 에 없음"):
            ExtractionFieldDef(
                field_name="status",
                field_type="enum",
                required=False,
                description="상태",
                enum_values=["draft", "active", "archived"],
                default_value="deleted",
            )

    def test_enum_rejects_non_string(self):
        with pytest.raises(ValidationError, match="문자열이어야"):
            ExtractionFieldDef(
                field_name="status",
                field_type="enum",
                required=False,
                description="상태",
                enum_values=["draft", "active"],
                default_value=1,
            )

    # --- array ------------------------------------------------------------

    def test_array_accepts_list(self):
        f = ExtractionFieldDef(
            field_name="tags",
            field_type="array",
            required=False,
            description="태그 목록",
            default_value=["legal", "finance"],
        )
        assert f.default_value == ["legal", "finance"]

    def test_array_rejects_dict(self):
        with pytest.raises(ValidationError, match="리스트여야"):
            ExtractionFieldDef(
                field_name="tags",
                field_type="array",
                required=False,
                description="태그 목록",
                default_value={"a": 1},
            )

    def test_array_rejects_string(self):
        with pytest.raises(ValidationError, match="리스트여야"):
            ExtractionFieldDef(
                field_name="tags",
                field_type="array",
                required=False,
                description="태그 목록",
                default_value="legal,finance",
            )

    # --- object -----------------------------------------------------------

    def test_object_accepts_dict_default(self):
        # object 는 nested_schema 가 필수이므로 함께 지정.
        f = ExtractionFieldDef(
            field_name="party",
            field_type="object",
            required=False,
            description="당사자",
            nested_schema={
                "name": ExtractionFieldDef(
                    field_name="name",
                    field_type="string",
                    required=True,
                    description="이름",
                )
            },
            default_value={"name": "Acme"},
        )
        assert f.default_value == {"name": "Acme"}

    def test_object_rejects_list_default(self):
        with pytest.raises(ValidationError, match="객체"):
            ExtractionFieldDef(
                field_name="party",
                field_type="object",
                required=False,
                description="당사자",
                nested_schema={
                    "name": ExtractionFieldDef(
                        field_name="name",
                        field_type="string",
                        required=True,
                        description="이름",
                    )
                },
                default_value=["Acme"],
            )
