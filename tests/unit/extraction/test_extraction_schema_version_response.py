"""
P5-2 서버측 `ExtractionSchemaVersionResponse.from_domain` 단위 테스트.

검증 포커스:
- `rolled_back_from_version` 필드가 `extra_metadata["rolled_back_from_version"]`
  에서 안전하게 뽑혀 1급 필드로 노출된다.
- 이상한 값(문자열, bool, 리스트, 음수, 0, None, 키 부재) 에서는 None 으로
  수렴한다 — 프론트가 혼란스러운 경고를 띄우지 않도록.
- 기존 필드(id, version, fields 등) 의 직렬화가 회귀 없이 유지된다.

이 테스트는 DB 없이 in-memory 도메인 객체(SimpleNamespace) 로 동작한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.extraction import ExtractionFieldDef
from app.schemas.extraction import ExtractionSchemaVersionResponse


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _leaf_field() -> ExtractionFieldDef:
    return ExtractionFieldDef(
        field_name="x",
        field_type="string",
        required=True,
        description="leaf field",
    )


def _make_domain(*, extra_metadata=None):
    """실제 도메인 객체(ExtractionSchemaVersion) 를 모사한 가벼운 namespace.

    from_domain 이 읽는 속성만 채운다.
    """
    return SimpleNamespace(
        id=uuid4(),
        schema_id=uuid4(),
        version=3,
        fields={"x": _leaf_field()},
        is_deprecated=False,
        deprecation_reason=None,
        change_summary="v2 로 되돌리기",
        changed_fields=["x"],
        created_at=datetime(2026, 4, 22, 12, 34, 56, tzinfo=timezone.utc),
        created_by="cck1835@gmail.com",
        # from_domain 은 extra_metadata 가 dict 가 아닐 수도 있는 경우를 감안.
        extra_metadata=extra_metadata,
    )


# ---------------------------------------------------------------------------
# rolled_back_from_version 추출 규칙
# ---------------------------------------------------------------------------


class TestRolledBackFromVersionExtraction:
    def test_extracts_positive_int(self):
        v = _make_domain(extra_metadata={"rolled_back_from_version": 2})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version == 2

    def test_missing_key_is_none(self):
        v = _make_domain(extra_metadata={})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_extra_metadata_none_is_none(self):
        v = _make_domain(extra_metadata=None)
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_string_value_rejected(self):
        """이상 값은 None 으로 수렴해야 한다 (프론트 혼선 방지)."""
        v = _make_domain(extra_metadata={"rolled_back_from_version": "2"})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_bool_value_rejected(self):
        """Python 에서 bool 은 int 서브타입이지만 의미상 bool → 버전 번호가 아님."""
        v = _make_domain(extra_metadata={"rolled_back_from_version": True})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_list_value_rejected(self):
        v = _make_domain(extra_metadata={"rolled_back_from_version": [2]})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_zero_rejected(self):
        """버전은 1 이상 — 0 은 이상치."""
        v = _make_domain(extra_metadata={"rolled_back_from_version": 0})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_negative_rejected(self):
        v = _make_domain(extra_metadata={"rolled_back_from_version": -1})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_extra_metadata_non_dict_is_safe(self):
        """extra_metadata 가 dict 가 아닌 이상한 값이어도 예외 없이 None 반환."""
        v = _make_domain(extra_metadata=["not", "a", "dict"])
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        assert dto.rolled_back_from_version is None

    def test_other_metadata_keys_ignored(self):
        """rolled_back_from_version 외의 키는 응답 DTO 로 누설되지 않아야 한다."""
        v = _make_domain(
            extra_metadata={
                "rolled_back_from_version": 2,
                "secret_internal_flag": "should-not-leak",
            }
        )
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        # rolled_back_from_version 은 전달됨
        assert dto.rolled_back_from_version == 2
        # extra_metadata 자체는 DTO 에 노출되지 않는다 (보안상)
        dumped = dto.model_dump()
        assert "extra_metadata" not in dumped
        assert "secret_internal_flag" not in dumped


# ---------------------------------------------------------------------------
# 회귀 — 기존 필드 직렬화
# ---------------------------------------------------------------------------


class TestVersionResponseSerialization:
    def test_serializes_core_fields(self):
        v = _make_domain(extra_metadata={"rolled_back_from_version": 2})
        dto = ExtractionSchemaVersionResponse.from_domain(v)
        data = dto.model_dump()
        assert data["version"] == 3
        assert data["changed_fields"] == ["x"]
        assert data["change_summary"] == "v2 로 되돌리기"
        assert data["is_deprecated"] is False
        assert data["created_by"] == "cck1835@gmail.com"
        # created_at 은 isoformat 문자열
        assert isinstance(data["created_at"], str)
        assert data["created_at"].startswith("2026-04-22T12:34:56")
        # fields 는 ExtractionFieldDef.model_dump() 결과
        assert "x" in data["fields"]
        assert data["fields"]["x"]["field_type"] == "string"

    def test_rolled_back_from_version_appears_in_dump(self):
        v = _make_domain(extra_metadata={"rolled_back_from_version": 2})
        dumped = ExtractionSchemaVersionResponse.from_domain(v).model_dump()
        assert dumped["rolled_back_from_version"] == 2

    def test_rolled_back_from_version_none_in_dump_for_non_rollback(self):
        v = _make_domain(extra_metadata={})
        dumped = ExtractionSchemaVersionResponse.from_domain(v).model_dump()
        assert dumped["rolled_back_from_version"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
