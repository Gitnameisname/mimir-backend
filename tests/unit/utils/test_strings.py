"""Unit tests for :mod:`app.utils.strings`.

Covers:
    - ``normalize_display_name``: 공백 압축 / 길이 상하한 / 슬래시 금지 / label 치환 /
      None 처리 / 대소문자 보존 / 엣지 케이스 (min==max, max_len=0).
    - ``normalize_lower``: None 패스스루 / strip→lower 순서 / 빈문자열 / 멀티바이트.
Docs: ``docs/함수도서관/backend.md`` §1.4.
"""
from __future__ import annotations

import pytest

from app.api.errors.exceptions import ApiValidationError
from app.utils.strings import normalize_display_name, normalize_lower


# --- None / empty --------------------------------------------------------


def test_none_raises_required() -> None:
    with pytest.raises(ApiValidationError, match="필수입니다"):
        normalize_display_name(None, 1, 200, label="폴더 이름")


def test_blank_collapses_to_empty_then_length_error() -> None:
    """공백만 들어오면 split() 이후 빈 문자열 → 길이 에러로 귀결."""
    with pytest.raises(ApiValidationError, match="자 사이여야 합니다"):
        normalize_display_name("   ", 1, 200, label="컬렉션 이름")


def test_tabs_and_newlines_are_collapsed() -> None:
    assert (
        normalize_display_name("a\t b\n\n c", 1, 200, label="이름") == "a b c"
    )


# --- whitespace compression ---------------------------------------------


def test_strips_leading_trailing_whitespace() -> None:
    assert (
        normalize_display_name("  hello   world  ", 1, 200, label="이름")
        == "hello world"
    )


def test_preserves_single_interior_spaces() -> None:
    assert normalize_display_name("a b c", 1, 200, label="이름") == "a b c"


def test_preserves_korean_and_case() -> None:
    assert (
        normalize_display_name("  HelloWorld ABC  ", 1, 200, label="이름")
        == "HelloWorld ABC"
    )
    assert (
        normalize_display_name("   컬렉션   이름  ", 1, 200, label="이름")
        == "컬렉션 이름"
    )


# --- length bounds -------------------------------------------------------


def test_too_short_raises_length_error() -> None:
    with pytest.raises(ApiValidationError, match=r"2~5자"):
        normalize_display_name("a", 2, 5, label="이름")


def test_too_long_raises_length_error() -> None:
    with pytest.raises(ApiValidationError, match=r"1~200자"):
        normalize_display_name("x" * 201, 1, 200, label="이름")


def test_exact_min_length_is_accepted() -> None:
    assert normalize_display_name("ab", 2, 5, label="이름") == "ab"


def test_exact_max_length_is_accepted() -> None:
    assert normalize_display_name("abcde", 2, 5, label="이름") == "abcde"


def test_min_equals_max_single_length() -> None:
    assert normalize_display_name("abc", 3, 3, label="이름") == "abc"
    with pytest.raises(ApiValidationError, match="3~3자"):
        normalize_display_name("abcd", 3, 3, label="이름")


# --- forbid_slash --------------------------------------------------------


def test_slash_forbidden_raises() -> None:
    with pytest.raises(ApiValidationError, match="'/' 는 포함할 수 없습니다"):
        normalize_display_name(
            "a/b", 1, 200, forbid_slash=True, label="폴더 이름",
        )


def test_slash_allowed_by_default() -> None:
    assert (
        normalize_display_name("a/b", 1, 200, label="컬렉션 이름") == "a/b"
    )


def test_slash_inside_collapsed_spaces_still_detected() -> None:
    """공백 압축 뒤에도 '/' 는 여전히 존재해야 탐지된다."""
    with pytest.raises(ApiValidationError, match="'/'"):
        normalize_display_name(
            "  a /  b  ", 1, 200, forbid_slash=True, label="폴더 이름",
        )


# --- label interpolation -------------------------------------------------


def test_label_is_used_in_required_error() -> None:
    with pytest.raises(ApiValidationError, match="^컬렉션 이름은 필수입니다$"):
        normalize_display_name(None, 1, 200, label="컬렉션 이름")


def test_label_is_used_in_length_error() -> None:
    with pytest.raises(
        ApiValidationError, match="^폴더 이름은 1~200자 사이여야 합니다$",
    ):
        normalize_display_name("", 1, 200, label="폴더 이름")


def test_label_is_used_in_slash_error() -> None:
    with pytest.raises(
        ApiValidationError, match="^폴더 이름에 '/' 는 포함할 수 없습니다$",
    ):
        normalize_display_name(
            "a/b", 1, 200, forbid_slash=True, label="폴더 이름",
        )


# --- regression guards ---------------------------------------------------


def test_no_side_effects_on_import() -> None:
    import app.utils.strings as mod

    assert set(mod.__all__) == {"normalize_display_name", "normalize_lower"}
    assert not hasattr(mod, "requests")
    assert not hasattr(mod, "httpx")


def test_kwargs_only_flags() -> None:
    """forbid_slash/label 은 keyword-only 여야 한다 (API 오남용 방지)."""
    with pytest.raises(TypeError):
        # 위치 인자로 넘기면 실패해야 함
        normalize_display_name("a/b", 1, 200, True, "폴더 이름")  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("x", "x"),
        ("   space   leading", "space leading"),
        ("trailing   space   ", "trailing space"),
        ("  multi    inner    gap  ", "multi inner gap"),
    ],
)
def test_parametrized_compression(raw: str, expected: str) -> None:
    assert normalize_display_name(raw, 1, 200, label="이름") == expected


# ===========================================================================
# normalize_lower (BE-G1, 2026-04-25)
# ===========================================================================


class TestNormalizeLower:
    def test_none_passthrough(self) -> None:
        assert normalize_lower(None) is None

    def test_empty_string_returns_empty(self) -> None:
        # 빈 문자열은 strip().lower() = "" — None 으로 변환하지 않는다
        assert normalize_lower("") == ""

    def test_whitespace_only_strips_to_empty(self) -> None:
        assert normalize_lower("   ") == ""

    def test_basic_lower(self) -> None:
        assert normalize_lower("ABC") == "abc"

    def test_mixed_case(self) -> None:
        assert normalize_lower("HelloWorld") == "helloworld"

    def test_strip_then_lower(self) -> None:
        assert normalize_lower("  Hello World  ") == "hello world"

    def test_inner_spaces_preserved(self) -> None:
        # 내부 공백은 보존 (normalize_display_name 과 다름)
        assert normalize_lower("  A  B  ") == "a  b"

    def test_email_like(self) -> None:
        assert normalize_lower("  USER@Example.COM ") == "user@example.com"

    def test_korean_unchanged(self) -> None:
        # 한글은 lower 영향 없음, strip 만 적용
        assert normalize_lower("  한글  ") == "한글"

    def test_unicode_preserved(self) -> None:
        # NFKC 같은 정규화는 적용 안 함 (의도)
        assert normalize_lower("Ⅰ") == "ⅰ"  # 로마 숫자 — Python str.lower 가 처리

    def test_idempotent(self) -> None:
        s = "  Hello  "
        once = normalize_lower(s)
        twice = normalize_lower(once)
        assert once == twice == "hello"
