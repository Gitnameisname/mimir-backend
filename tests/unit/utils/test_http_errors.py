"""Unit tests for :mod:`app.utils.http_errors`.

Covers: 4종(404/400/422/409) status_code · detail · X-Error-Code 헤더 머지 ·
    호출자 headers 보존 · error_code 부재 시 헤더 미부착 · 빈 detail 처리.
Docs: ``docs/함수도서관/backend.md`` §1.5 BE-G3.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.utils.http_errors import (
    bad_request,
    conflict,
    not_found,
    unprocessable_entity,
)


# ---------------------------------------------------------------------------
# 1. status_code 정확성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "expected_status"),
    [
        (not_found, 404),
        (bad_request, 400),
        (unprocessable_entity, 422),
        (conflict, 409),
    ],
)
def test_status_code(factory, expected_status) -> None:
    exc = factory("메시지")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == expected_status


# ---------------------------------------------------------------------------
# 2. detail 문자열 보존 (프런트 getApiErrorMessage 호환)
# ---------------------------------------------------------------------------


def test_detail_string_passthrough() -> None:
    exc = not_found("문서를 찾을 수 없습니다.")
    assert exc.detail == "문서를 찾을 수 없습니다."


def test_detail_empty_string_allowed() -> None:
    """라우터가 빈 detail 을 의도적으로 보낼 수 있음 — 차단하지 않는다."""
    exc = bad_request("")
    assert exc.detail == ""


# ---------------------------------------------------------------------------
# 3. X-Error-Code 헤더 자동 부착
# ---------------------------------------------------------------------------


def test_error_code_emits_header() -> None:
    exc = not_found("없음", error_code="NOT_FOUND_DOCUMENT")
    assert exc.headers is not None
    assert exc.headers.get("X-Error-Code") == "NOT_FOUND_DOCUMENT"


def test_no_error_code_no_headers() -> None:
    """error_code/headers 둘 다 없으면 headers 자체가 None — FastAPI 기본 응답 유지."""
    exc = not_found("없음")
    assert exc.headers is None


def test_error_code_with_user_headers_merged() -> None:
    exc = conflict(
        "충돌",
        error_code="CONFLICT_VERSION",
        headers={"Retry-After": "5"},
    )
    assert exc.headers is not None
    assert exc.headers.get("X-Error-Code") == "CONFLICT_VERSION"
    assert exc.headers.get("Retry-After") == "5"


def test_user_headers_overrides_error_code_header_on_conflict() -> None:
    """호출자가 X-Error-Code 를 직접 명시하면 그 값이 우선."""
    exc = bad_request(
        "안 좋음",
        error_code="AUTO_GENERATED",
        headers={"X-Error-Code": "EXPLICIT_USER_CHOICE"},
    )
    assert exc.headers is not None
    assert exc.headers.get("X-Error-Code") == "EXPLICIT_USER_CHOICE"


def test_user_headers_only_no_error_code() -> None:
    exc = unprocessable_entity("거부", headers={"X-Trace-Id": "tr-1"})
    assert exc.headers is not None
    assert exc.headers.get("X-Trace-Id") == "tr-1"
    assert "X-Error-Code" not in exc.headers


# ---------------------------------------------------------------------------
# 4. 4 함수 모두 같은 인터페이스 일관성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [not_found, bad_request, unprocessable_entity, conflict],
)
def test_keyword_only_args(factory) -> None:
    """error_code / headers 는 keyword-only — positional 호출은 TypeError."""
    with pytest.raises(TypeError):
        factory("메시지", "POSITIONAL_ERROR_CODE")  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [not_found, bad_request, unprocessable_entity, conflict],
)
def test_returns_not_raises(factory) -> None:
    """본 함수들은 인스턴스만 만들고 raise 는 호출자 책임."""
    exc = factory("메시지")
    # raise 하지 않았다 — 객체로 반환됨
    assert isinstance(exc, HTTPException)


# ---------------------------------------------------------------------------
# 5. 한국어 메시지 보존
# ---------------------------------------------------------------------------


def test_korean_detail_preserved() -> None:
    exc = unprocessable_entity("문서 타입이 유효하지 않습니다 (UPPER_SNAKE).")
    assert exc.detail == "문서 타입이 유효하지 않습니다 (UPPER_SNAKE)."


# ---------------------------------------------------------------------------
# 6. B-N4 (2026-04-25) — not_found_resource 도메인 변형
# ---------------------------------------------------------------------------
#
# not_found (HTTPException) 와는 다른 계층 — ApiNotFoundError 인스턴스를 만든다.
# handlers.api_error_handler 가 404 + error_code="resource_not_found" 응답으로 변환.


from app.api.errors.exceptions import ApiNotFoundError  # noqa: E402

from app.utils.http_errors import not_found_resource  # noqa: E402


def test_not_found_resource_returns_api_not_found_error() -> None:
    """HTTPException 이 아닌 ApiNotFoundError 를 반환해야 한다."""
    exc = not_found_resource("문서", "doc-42")
    assert isinstance(exc, ApiNotFoundError)
    assert not isinstance(exc, HTTPException)


def test_not_found_resource_message_format_korean() -> None:
    """표준 메시지 포맷: ``{label_ko}을(를) 찾을 수 없습니다: {resource_id}``."""
    exc = not_found_resource("문서", "doc-42")
    assert exc.message == "문서을(를) 찾을 수 없습니다: doc-42"


def test_not_found_resource_http_status_404() -> None:
    exc = not_found_resource("버전", "ver-1")
    assert exc.http_status == 404


def test_not_found_resource_default_error_code() -> None:
    """error_code 미지정 시 ApiNotFoundError 클래스 default 사용."""
    exc = not_found_resource("폴더", "fld-7")
    assert exc.error_code == "resource_not_found"


def test_not_found_resource_error_code_override() -> None:
    """error_code 지정 시 인스턴스 attribute 로 override."""
    exc = not_found_resource(
        "컬렉션",
        "col-3",
        error_code="NOT_FOUND_COLLECTION",
    )
    assert exc.error_code == "NOT_FOUND_COLLECTION"
    # 클래스 default 는 변하지 않음
    assert ApiNotFoundError.error_code == "resource_not_found"


def test_not_found_resource_details_structure() -> None:
    """details 에 field/reason/label/resource_id 4 키가 들어 있다."""
    exc = not_found_resource("문서", "doc-99")
    assert isinstance(exc.details, list)
    assert len(exc.details) == 1
    detail = exc.details[0]
    assert detail == {
        "field": "resource_id",
        "reason": "not found",
        "label": "문서",
        "resource_id": "doc-99",
    }


@pytest.mark.parametrize(
    ("label_ko", "resource_id"),
    [
        ("문서", "doc-1"),
        ("버전", "ver-2"),
        ("폴더", "fld-3"),
        ("컬렉션", "col-4"),
        ("스키마", "sch-5"),
    ],
)
def test_not_found_resource_24_사이트_라벨_시나리오(label_ko, resource_id) -> None:
    """24 사이트 도메인 라벨 (Document/Version/Folder/Collection/기타) 시나리오."""
    exc = not_found_resource(label_ko, resource_id)
    assert exc.message == f"{label_ko}을(를) 찾을 수 없습니다: {resource_id}"
    assert exc.details[0]["label"] == label_ko
    assert exc.details[0]["resource_id"] == resource_id


def test_not_found_resource_keyword_only_error_code() -> None:
    """error_code 는 keyword-only — positional 호출은 TypeError."""
    with pytest.raises(TypeError):
        not_found_resource("문서", "doc-1", "POSITIONAL_CODE")  # type: ignore[misc]


def test_not_found_resource_does_not_raise() -> None:
    """본 함수는 인스턴스만 만들고 raise 는 호출자 책임."""
    exc = not_found_resource("문서", "doc-1")
    assert isinstance(exc, ApiNotFoundError)
    # raise 되지 않았음 — 호출 자체가 통과
