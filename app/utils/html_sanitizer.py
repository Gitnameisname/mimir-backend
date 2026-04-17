"""HTML sanitization utility — XSS 방어."""
from __future__ import annotations

import re

# 허용 태그 최소 집합
_ALLOWED_TAGS = {"p", "br", "strong", "em", "u", "ul", "ol", "li", "a"}

# 허용 속성 (a 태그만)
_ALLOWED_ATTRS = {"a": {"href", "title"}}

# 위험한 태그 패턴
_DANGEROUS_TAG_RE = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|input|button|svg|math)"
    r"[\s>]",
    re.IGNORECASE,
)

# event handler 속성 패턴
_EVENT_HANDLER_RE = re.compile(r"\bon\w+\s*=", re.IGNORECASE)

# javascript: 프로토콜 패턴
_JS_PROTO_RE = re.compile(r"javascript\s*:", re.IGNORECASE)

# data: URI 패턴
_DATA_URI_RE = re.compile(r"data\s*:[^;]+;base64", re.IGNORECASE)


def sanitize_html(text: str) -> str:
    """HTML 문자열에서 위험 요소를 제거한다.

    완전한 HTML 파서 없이 regex 기반으로 빠르게 처리.
    완전한 sanitization이 필요하면 bleach 등 전용 라이브러리 사용 권장.
    """
    if not text:
        return text

    # 1. 위험 태그 블록 전체 제거
    result = re.sub(
        r"<\s*(script|style|iframe|object|embed|form|input|button|svg|math)"
        r"[\s\S]*?</\s*\1\s*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # 셀프 클로징 위험 태그 제거
    result = re.sub(
        r"<\s*(script|style|iframe|object|embed|form|input|button|svg|math)[^>]*/?>",
        "",
        result,
        flags=re.IGNORECASE,
    )

    # 2. javascript: 프로토콜 제거
    result = _JS_PROTO_RE.sub("", result)

    # 3. data: URI 제거
    result = _DATA_URI_RE.sub("", result)

    # 4. event handler 속성 제거
    result = _EVENT_HANDLER_RE.sub("", result)

    return result


def escape_html(text: str) -> str:
    """HTML 특수 문자를 엔티티로 이스케이프한다."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
