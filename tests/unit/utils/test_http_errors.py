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
