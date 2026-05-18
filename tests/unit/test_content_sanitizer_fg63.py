"""S3 Phase 6 FG 6-3: content_sanitizer 단위 회귀.

회귀 시나리오:
  S1. null byte (\x00) reject — write 거부.
  S2. BOM / RLO override reject.
  S3. 정상 한글 / 이모지 통과.
  S4. ANSI escape sequence sanitize (응답 단계 제거).
  S5. C0 control char 제거, 단 tab/newline/CR 보존.
  S6. 길이 초과 reject.
"""
from __future__ import annotations

import pytest

from app.api.errors.exceptions import ApiValidationError
from app.utils.content_sanitizer import (
    MAX_CONTENT_LENGTH,
    reject_dangerous_chars,
    sanitize_for_response,
)


# -- write 단계 reject --------------------------------------------------------

def test_reject_null_byte():
    with pytest.raises(ApiValidationError):
        reject_dangerous_chars("hello\x00world")


def test_reject_bom():
    with pytest.raises(ApiValidationError):
        reject_dangerous_chars("﻿header")


def test_reject_rlo_override():
    with pytest.raises(ApiValidationError):
        reject_dangerous_chars("text‮suffix")


def test_reject_surrogate():
    text = "preview" + chr(0xD800) + "tail"
    with pytest.raises(ApiValidationError):
        reject_dangerous_chars(text)


def test_reject_length_overflow():
    with pytest.raises(ApiValidationError):
        reject_dangerous_chars("x" * (MAX_CONTENT_LENGTH + 1))


def test_accept_korean_and_emoji():
    text = "안녕하세요 🇰🇷 — 한글 NFC/NFD 모두 통과되어야 한다."
    assert reject_dangerous_chars(text) == text


def test_accept_whitespace_and_newlines():
    text = "line1\nline2\tcell\rsuffix"
    assert reject_dangerous_chars(text) == text


def test_none_input_returns_empty():
    assert reject_dangerous_chars(None) == ""


# -- read 단계 sanitize -------------------------------------------------------

def test_sanitize_ansi_csi_color():
    # ESC[31m red ESC[0m
    raw = "before \x1b[31mRED\x1b[0m after"
    assert sanitize_for_response(raw) == "before RED after"


def test_sanitize_ansi_osc():
    raw = "title \x1b]0;window title\x07 stays"
    assert sanitize_for_response(raw) == "title  stays"


def test_sanitize_control_chars():
    raw = "alpha\x01\x02beta\x7f"
    assert sanitize_for_response(raw) == "alphabeta"


def test_preserve_tab_newline_cr():
    raw = "row1\tcol2\r\nrow2"
    assert sanitize_for_response(raw) == raw


def test_sanitize_keeps_unicode():
    raw = "한국어\x1b[1mABC\x1b[0m 이모지 🚀"
    assert sanitize_for_response(raw) == "한국어ABC 이모지 🚀"


def test_sanitize_none():
    assert sanitize_for_response(None) == ""
