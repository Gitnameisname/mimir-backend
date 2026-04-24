"""
Extraction Schema 요청 Pydantic 검증 단위 테스트 — Phase 8 FG8.1 (P2-C / P3-C)

대상: backend/app/schemas/extraction.py

테스트 범위:
- CreateExtractionSchemaRequest.doc_type_code regex 거절
- scope_profile_id 비-UUID 거절 (빈 문자열은 None 으로 수렴)
- change_summary / reason 제어문자·공백-only 거절
- fields 비어있으면 거절
- fields 최상위 개수 상한(MAX_FIELDS_COUNT=200) 초과 거절 (P3-C)
- nested_schema 깊이 상한(MAX_NESTED_DEPTH=3) 초과 거절 (P3-C)
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.extraction import (
    MAX_FIELDS_COUNT,
    MAX_NESTED_DEPTH,
    CreateExtractionSchemaRequest,
    DeprecateExtractionSchemaRequest,
    UpdateExtractionSchemaRequest,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _string_field(name: str = "x", *, description: str = "desc") -> dict:
    return {
        "field_name": name,
        "field_type": "string",
        "required": True,
        "description": description,
    }


def _build_nested_object_chain(depth: int) -> dict:
    """nested_schema 를 `depth` 단계로 내려가는 object 필드 dict 를 만든다.

    depth=0 → 리프 string 필드
    depth=N → object 필드 → nested_schema={ inner: _build_nested_object_chain(N-1) }
    """
    if depth <= 0:
        return _string_field("leaf")
    return {
        "field_name": "obj",
        "field_type": "object",
        "required": False,
        "description": "nested object",
        "nested_schema": {"inner": _build_nested_object_chain(depth - 1)},
    }


# ---------------------------------------------------------------------------
# CreateExtractionSchemaRequest
# ---------------------------------------------------------------------------


class TestDocTypeCode:
    def test_valid_alpha_start(self):
        # P7-2 에서 doc_type_code 는 대문자로 정규화됨 (upper).
        req = CreateExtractionSchemaRequest(
            doc_type_code="Contract-v1",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "CONTRACT-V1"

    def test_rejects_numeric_start(self):
        with pytest.raises(ValidationError):
            CreateExtractionSchemaRequest(
                doc_type_code="1contract",
                fields={"x": _string_field("x")},
            )

    def test_rejects_slash(self):
        with pytest.raises(ValidationError):
            CreateExtractionSchemaRequest(
                doc_type_code="contract/v1",
                fields={"x": _string_field("x")},
            )

    def test_rejects_empty(self):
        with pytest.raises(ValidationError):
            CreateExtractionSchemaRequest(
                doc_type_code="",
                fields={"x": _string_field("x")},
            )

    def test_strips_leading_trailing_spaces(self):
        # 공백 제거 + 대문자 정규화 (P7-2).
        req = CreateExtractionSchemaRequest(
            doc_type_code="  contract  ",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "CONTRACT"


class TestScopeProfileId:
    def test_valid_uuid(self):
        req = CreateExtractionSchemaRequest(
            doc_type_code="x",
            fields={"x": _string_field("x")},
            scope_profile_id="00000000-0000-4000-8000-000000000001",
        )
        assert req.scope_profile_id == "00000000-0000-4000-8000-000000000001"

    def test_empty_string_becomes_none(self):
        req = CreateExtractionSchemaRequest(
            doc_type_code="x",
            fields={"x": _string_field("x")},
            scope_profile_id="",
        )
        assert req.scope_profile_id is None

    def test_rejects_non_uuid_string(self):
        with pytest.raises(ValidationError):
            CreateExtractionSchemaRequest(
                doc_type_code="x",
                fields={"x": _string_field("x")},
                scope_profile_id="not-a-uuid",
            )


class TestFieldsBounds:
    def test_rejects_empty_fields(self):
        with pytest.raises(ValidationError):
            CreateExtractionSchemaRequest(doc_type_code="x", fields={})

    def test_accepts_at_upper_bound(self):
        n = MAX_FIELDS_COUNT
        fields = {
            f"f_{i}": _string_field(f"f_{i}") for i in range(n)
        }
        req = CreateExtractionSchemaRequest(doc_type_code="x", fields=fields)
        assert len(req.fields) == n

    def test_rejects_over_max_fields_count(self):
        n = MAX_FIELDS_COUNT + 1
        fields = {
            f"f_{i}": _string_field(f"f_{i}") for i in range(n)
        }
        with pytest.raises(ValidationError) as exc:
            CreateExtractionSchemaRequest(doc_type_code="x", fields=fields)
        assert str(MAX_FIELDS_COUNT) in str(exc.value)

    def test_accepts_nested_depth_at_bound(self):
        # depth 3 → 최상위 object → inner object → inner object → leaf string
        root = _build_nested_object_chain(MAX_NESTED_DEPTH)
        req = CreateExtractionSchemaRequest(
            doc_type_code="x",
            fields={"root": root},
        )
        assert "root" in req.fields

    def test_rejects_nested_depth_over_max(self):
        root = _build_nested_object_chain(MAX_NESTED_DEPTH + 1)
        with pytest.raises(ValidationError) as exc:
            CreateExtractionSchemaRequest(
                doc_type_code="x",
                fields={"root": root},
            )
        assert "깊이" in str(exc.value) or "depth" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# UpdateExtractionSchemaRequest
# ---------------------------------------------------------------------------


class TestUpdateRequest:
    def test_change_summary_whitespace_becomes_none(self):
        req = UpdateExtractionSchemaRequest(
            fields={"x": _string_field("x")},
            change_summary="   ",
        )
        assert req.change_summary is None

    def test_change_summary_rejects_control_char(self):
        with pytest.raises(ValidationError):
            UpdateExtractionSchemaRequest(
                fields={"x": _string_field("x")},
                change_summary="hello\x00there",
            )

    def test_change_summary_accepts_normal_text(self):
        req = UpdateExtractionSchemaRequest(
            fields={"x": _string_field("x")},
            change_summary=" party_b 필드 추가 ",
        )
        # 앞뒤 공백은 strip 되어야 함
        assert req.change_summary == "party_b 필드 추가"

    def test_fields_empty_rejected(self):
        with pytest.raises(ValidationError):
            UpdateExtractionSchemaRequest(fields={})


# ---------------------------------------------------------------------------
# DeprecateExtractionSchemaRequest
# ---------------------------------------------------------------------------


class TestDeprecateRequest:
    def test_accepts_normal_reason(self):
        req = DeprecateExtractionSchemaRequest(reason="2026-05 계약 양식 변경")
        assert req.reason == "2026-05 계약 양식 변경"

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValidationError):
            DeprecateExtractionSchemaRequest(reason="     ")

    def test_rejects_control_char(self):
        with pytest.raises(ValidationError):
            DeprecateExtractionSchemaRequest(reason="stop\x1bnow")

    def test_rejects_del_char(self):
        with pytest.raises(ValidationError):
            DeprecateExtractionSchemaRequest(reason="foo\x7f")
