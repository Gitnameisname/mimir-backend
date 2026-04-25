"""Unit tests for :mod:`app.utils.converters`.

Covers:
    - ``uuid_str_or_none``: UUID/str/None 입력 / falsy 처리 / 형식 미검증.
    - ``ensure_uuid``: UUID/str 정상 / 잘못된 입력 → ApiValidationError + details + code.
Docs: ``docs/함수도서관/backend.md`` §1.2 BE-G1.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from app.api.errors.exceptions import ApiValidationError
from app.utils.converters import ensure_uuid, uuid_str_or_none

_VALID_UUID_STR = "550e8400-e29b-41d4-a716-446655440000"
_VALID_UUID = UUID(_VALID_UUID_STR)


# ---------------------------------------------------------------------------
# uuid_str_or_none
# ---------------------------------------------------------------------------


class TestUuidStrOrNone:
    def test_uuid_instance(self) -> None:
        assert uuid_str_or_none(_VALID_UUID) == _VALID_UUID_STR

    def test_uuid_string(self) -> None:
        assert uuid_str_or_none(_VALID_UUID_STR) == _VALID_UUID_STR

    def test_none(self) -> None:
        assert uuid_str_or_none(None) is None

    def test_empty_string_returns_none(self) -> None:
        """기존 보일러 ``str(x) if x else None`` 의미 보존: 빈 문자열은 falsy → None."""
        assert uuid_str_or_none("") is None

    def test_no_format_validation(self) -> None:
        """본 함수는 형식 검증을 하지 않는다 — 호출자가 보장하거나 ensure_uuid 사용."""
        # 잘못된 형식이라도 truthy 면 str 캐스팅 결과 반환
        assert uuid_str_or_none("not-a-uuid") == "not-a-uuid"


# ---------------------------------------------------------------------------
# ensure_uuid
# ---------------------------------------------------------------------------


class TestEnsureUuid:
    def test_uuid_instance_passthrough(self) -> None:
        assert ensure_uuid(_VALID_UUID) is _VALID_UUID

    def test_string_to_uuid(self) -> None:
        result = ensure_uuid(_VALID_UUID_STR)
        assert isinstance(result, UUID)
        assert str(result) == _VALID_UUID_STR

    def test_invalid_string_raises_api_validation_error(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("not-a-uuid")
        # 메시지에 한국어 + 라벨 포함
        assert "올바른 UUID 형식이 아닙니다" in str(exc_info.value.message)

    def test_none_raises_api_validation_error(self) -> None:
        # None 은 isinstance(UUID) 도 아니고 UUID(None) 도 TypeError → 잡아서 ValidationError
        with pytest.raises(ApiValidationError):
            ensure_uuid(None)  # type: ignore[arg-type]

    def test_label_appears_in_message_and_details(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("bad", label="document_id")
        err = exc_info.value
        assert "document_id" in err.message
        assert err.details and isinstance(err.details, list)
        d0 = err.details[0]
        assert d0["field"] == "document_id"
        assert d0["reason"] == "must be a valid UUID"
        assert d0["code"] == "INVALID_UUID"

    def test_default_label_is_id(self) -> None:
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("xxx")
        assert "id" in exc_info.value.message
        assert exc_info.value.details[0]["field"] == "id"

    def test_keyword_only_label(self) -> None:
        """label 은 keyword-only — positional 호출은 TypeError."""
        with pytest.raises(TypeError):
            ensure_uuid(_VALID_UUID_STR, "document_id")  # type: ignore[misc]

    def test_chained_exception_preserves_root_cause(self) -> None:
        """ApiValidationError.__cause__ 가 원본 ValueError/TypeError 를 보존."""
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("bad")
        assert exc_info.value.__cause__ is not None

    # ─── D5 (2026-04-25): status_code 옵션 ───

    def test_default_status_code_400(self) -> None:
        """기본 status_code 는 400 (ApiValidationError 의 클래스 default)."""
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("bad")
        assert exc_info.value.http_status == 400

    def test_status_code_422_override(self) -> None:
        """status_code=422 옵션으로 인스턴스 http_status override."""
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("bad", status_code=422)
        assert exc_info.value.http_status == 422
        # 클래스 default 는 변하지 않음 (인스턴스만 영향)
        assert ApiValidationError.http_status == 400

    def test_status_code_does_not_affect_other_fields(self) -> None:
        """status_code override 가 message/details/code 영향 없음."""
        with pytest.raises(ApiValidationError) as exc_info:
            ensure_uuid("bad", label="doc_id", status_code=422)
        err = exc_info.value
        assert err.http_status == 422
        assert "doc_id" in err.message
        assert err.details[0]["code"] == "INVALID_UUID"
