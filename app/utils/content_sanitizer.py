"""콘텐츠 sanitize 유틸 — S3 Phase 6 FG 6-3 (2026-05-18).

본 모듈은 사용자 텍스트 입력 (annotation content / mention payload 등) 에
포함될 수 있는 **저수준 바이트 위험** 을 차단하기 위한 단일 진입점이다.

설계 원칙 (Phase 6 §1.2 R-O3):
  - **write 시 reject** — null byte, BOM, U+202E (RLO) 등 명백한 위험 문자 는
    저장 자체를 거부 (``ApiValidationError``).
  - **read 시 sanitize** — ANSI escape sequence / 기타 C0/C1 control char
    (단, ``\\t`` / ``\\n`` / ``\\r`` 은 보존) 는 응답 직렬화 단계에서 제거.
  - **원본 보존** — 그 외 문자는 raw 그대로 DB 저장 (사용자 의도 보존, R-O3 원칙).
  - **한글 NFC/NFD 보존** — BMP/Supplementary 한국어 문자 는 전부 통과 (R-03 회귀).

기존 `app.utils.html_sanitizer` 과 별 layer:
  - html_sanitizer  : HTML 태그 / JS 프로토콜 제거 (XSS).
  - content_sanitizer: 텍스트 바이트 수준 제어 문자 / 위험 코드포인트 제거.

본 모듈은 Phase 4 의 prompt-injection (LLM 명령 해석 layer) 와 충돌하지 않는다 —
Phase 6 §7 R-07 참고.
"""
from __future__ import annotations

import re

from app.api.errors.exceptions import ApiValidationError

__all__ = [
    "reject_dangerous_chars",
    "sanitize_for_response",
    "MAX_CONTENT_LENGTH",
]

# annotations.content_length 제약 과 동치 (DB CHECK 와 정합).
MAX_CONTENT_LENGTH = 10_000

# write 시 reject 대상 코드포인트:
#   - U+0000 NULL  — Postgres TEXT 가 거부, C-string 종료 문자.
#   - U+FEFF BOM   — 본문 안에 있으면 직렬화/검색에서 비정상.
#   - U+202E RIGHT-TO-LEFT OVERRIDE — 표시 wrapping 공격.
#   - U+202D LEFT-TO-RIGHT OVERRIDE — 같은 계열.
#   - U+200E/U+200F LEFT/RIGHT-TO-LEFT MARK — 같은 계열.
#   - Surrogate (U+D800..U+DFFF) — UTF-8 표현 불가 (Postgres 거부).
_REJECT_CODEPOINTS = frozenset(
    {
        "\x00",     # NULL
        "﻿",   # BOM
        "‮",   # RIGHT-TO-LEFT OVERRIDE
        "‭",   # LEFT-TO-RIGHT OVERRIDE
        "‎",   # LEFT-TO-RIGHT MARK
        "‏",   # RIGHT-TO-LEFT MARK
    }
)


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def reject_dangerous_chars(
    text: str | None,
    *,
    field_label: str = "content",
    max_length: int = MAX_CONTENT_LENGTH,
) -> str:
    """write 단계 검증 — 위험 문자가 있으면 ``ApiValidationError``.

    :param text: 검사 대상 문자열. ``None`` 은 빈 문자열로 정규화.
    :param field_label: 에러 메시지에 들어갈 필드 이름.
    :param max_length: 길이 상한 (기본 ``MAX_CONTENT_LENGTH``).
    :returns: 원본 그대로 (sanitize 하지 않음 — R-O3 "write 시 raw 보존").
    :raises ApiValidationError: 위험 문자 검출 또는 길이 초과.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        raise ApiValidationError(f"{field_label} 은 문자열이어야 합니다.")
    if len(text) > max_length:
        raise ApiValidationError(
            f"{field_label} 가 너무 깁니다 (최대 {max_length}자)."
        )

    for ch in _REJECT_CODEPOINTS:
        if ch in text:
            raise ApiValidationError(
                f"{field_label} 에 허용되지 않은 제어 문자가 포함되어 있습니다."
            )

    if _has_surrogate(text):
        raise ApiValidationError(
            f"{field_label} 에 허용되지 않은 surrogate 코드포인트가 포함되어 있습니다."
        )

    return text


# read 시 sanitize 패턴:
#   - ANSI escape sequences (CSI / OSC / 기타) — `ESC [ ... letter` / `ESC ] ... BEL`
#     터미널 색 코드. 응답 본문에서는 의미 없는 잡음. 단, ESC (U+001B) 자체를
#     남기지 않는다.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER_RE = re.compile(r"\x1b[@-_]")  # 단일 letter (Fp/Fe escapes)

# 일반 control characters 제거 — \t (0x09) / \n (0x0A) / \r (0x0D) 만 보존.
_OTHER_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_response(text: str | None) -> str:
    """read 단계 sanitize — ANSI escape + control byte 제거.

    :param text: 정규화 대상 문자열. ``None`` 은 빈 문자열로.
    :returns: ANSI escape sequence 와 (탭/개행/캐리지 제외) C0/C1 control char
        가 제거된 문자열. 한글 / BMP 문자 / Supplementary 이모지 는 그대로.

    이 함수는 raw 본문을 변형해 **응답 직렬화** 에 사용된다 (DB 본문 그대로 두고).
    Phase 6 §1.2 R-O3 의 "read 시 sanitize, write 시 raw 보존" 원칙.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    result = _ANSI_OSC_RE.sub("", text)
    result = _ANSI_CSI_RE.sub("", result)
    result = _ANSI_OTHER_RE.sub("", result)
    result = _OTHER_CTRL_RE.sub("", result)
    return result
