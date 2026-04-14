"""
보안 헤더 미들웨어 (OWASP 권장 기준).

추가 헤더:
  - X-Content-Type-Options    : nosniff — MIME 스니핑 방지
  - X-Frame-Options           : DENY — Clickjacking 방지
  - X-XSS-Protection          : 0 — 구형 브라우저 XSS 필터 비활성화 (CSP 우선)
  - Strict-Transport-Security : HTTPS 강제 (production 전용)
  - Content-Security-Policy   : XSS/인젝션 방지
  - Referrer-Policy           : 정보 유출 최소화
  - Permissions-Policy        : 불필요한 브라우저 기능 비활성화
  - Cache-Control             : API 응답 캐시 비활성화

제거 헤더:
  - Server                    : 서버 소프트웨어 정보 노출 방지 (VULN-HEADER-01)
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

# CSP 기본 정책 (API 서버 — 문서/스크립트 응답 없음)
_CSP_API = (
    "default-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

_HSTS_MAX_AGE = 63_072_000  # 2년 (초)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """모든 응답에 보안 헤더를 추가하고, Server 헤더를 제거한다."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response: Response = await call_next(request)

        # MIME 스니핑 방지
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Clickjacking 방지
        response.headers["X-Frame-Options"] = "DENY"

        # 구형 브라우저 XSS 필터 비활성화 (CSP 우선 사용)
        response.headers["X-XSS-Protection"] = "0"

        # Content-Security-Policy
        response.headers["Content-Security-Policy"] = _CSP_API

        # Referrer 정보 최소화
        response.headers["Referrer-Policy"] = "no-referrer"

        # 불필요한 브라우저 기능 비활성화
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )

        # API 응답은 캐시하지 않는다
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"

        # HSTS — production 전용 (개발 환경에서 HTTPS 미사용)
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                f"max-age={_HSTS_MAX_AGE}; includeSubDomains; preload"
            )

        # Server 헤더 제거 (서버 소프트웨어 노출 방지)
        for key in ("server", "Server"):
            if key in response.headers:
                del response.headers[key]

        return response
