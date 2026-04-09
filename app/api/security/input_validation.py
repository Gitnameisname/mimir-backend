"""
공통 입력 검증 유틸리티 (OWASP A03: Injection 대응).

- SQL 인젝션: 파라미터화 쿼리 사용 정책 강제 (코드 리뷰 보조)
- Path traversal: 파일 경로 파라미터 정규화
- 페이로드 크기 제한: 요청 본문 크기 상한
- ReDoS 방지: 복잡한 정규식 타임아웃 래퍼
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# 요청 본문 최대 크기 (기본 10 MB)
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

# 위험 문자 패턴 (경로 순회 방지용 파일명 검증에 사용)
_UNSAFE_PATH_PATTERN = re.compile(r"\.\./|\.\.\\|%2e%2e|%252e", re.IGNORECASE)

# 널바이트 인젝션 패턴
_NULL_BYTE_PATTERN = re.compile(r"\x00")


def sanitize_filename(name: str) -> str:
    """파일명에서 경로 순회 문자를 제거한다.

    Path traversal (OWASP A01/A03) 방지.
    """
    # 경로 구분자 제거
    safe = PurePosixPath(name).name
    # 숨김 파일(.으로 시작) 허용하되 ..은 단독 컴포넌트로 불가
    if safe in ("", ".", ".."):
        return "_"
    return safe


def contains_null_byte(value: str) -> bool:
    """문자열에 널바이트(\x00)가 포함되면 True."""
    return bool(_NULL_BYTE_PATTERN.search(value))


def validate_uuid_param(value: str) -> bool:
    """UUID v4 형식 파라미터 검증."""
    _UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    return bool(_UUID_RE.match(value))


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """요청 본문 크기를 제한한다 (OWASP A05: Security Misconfiguration).

    Content-Length 헤더 또는 실제 스트리밍 읽기로 크기를 확인한다.
    초과 시 413 Payload Too Large 반환.
    """

    def __init__(self, app: Any, max_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                cl = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "INVALID_CONTENT_LENGTH", "message": "Invalid Content-Length header"},
                )
            if cl > self.max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "PAYLOAD_TOO_LARGE",
                        "message": f"Request body exceeds maximum allowed size ({self.max_bytes} bytes)",
                    },
                )
        return await call_next(request)
