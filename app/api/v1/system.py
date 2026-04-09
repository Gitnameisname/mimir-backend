"""
System router — /api/v1/system

운영성 endpoint를 제공한다.
  - GET /api/v1/system/health        : 헬스체크 (공개)
  - GET /api/v1/system/info         : 서비스 메타 정보 (공개)
  - GET /api/v1/system/metrics      : Prometheus 메트릭 (Phase 13-3)
  - GET /api/v1/system/error-test   : 공통 오류 처리 검증용 stub (Task I-3)

이 router는 인증을 요구하지 않는 공개 endpoint만 포함한다.
향후 내부 전용 운영 endpoint가 필요하면 /admin 하위로 분리한다.
"""
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.errors import (
    ApiAuthenticationError,
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiValidationError,
)
from app.api.responses import SuccessResponse, success_response
from app.config import settings
from app.observability.metrics import generate_metrics_text

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/health",
    summary="헬스체크",
    description="서비스 가용 여부를 확인한다.",
    response_model=SuccessResponse,
)
def health_check() -> SuccessResponse:
    return success_response(data={"healthy": True})


@router.get(
    "/info",
    summary="서비스 메타 정보",
    description="서비스 이름, API 버전, 실행 환경 등 기본 메타 정보를 반환한다.",
    response_model=SuccessResponse,
)
def service_info() -> SuccessResponse:
    return success_response(
        data={
            "service": settings.service_name,
            "api_version": settings.api_version,
            "environment": settings.environment,
        }
    )


@router.get(
    "/metrics",
    summary="Prometheus 메트릭",
    description=(
        "Prometheus scrape 형식으로 HTTP 요청/오류/응답 시간 메트릭을 반환한다.\n\n"
        "production 환경에서는 내부망 또는 별도 인증 레이어(nginx basic auth 등)로 보호해야 한다."
    ),
    tags=["system"],
    response_class=PlainTextResponse,
    include_in_schema=False,
)
def prometheus_metrics(request: Request) -> PlainTextResponse:
    # production 환경에서는 루프백/내부 IP에서만 scrape를 허용한다.
    # 외부 요청은 nginx/gateway 레이어에서 필터링해야 하지만,
    # 방어 심층(defense-in-depth)으로 애플리케이션 레벨에서도 검사한다.
    if settings.environment == "production":
        client_ip = request.client.host if request.client else ""
        # VULN-P13-01 수정: X-Forwarded-For를 우선하면 공격자가 헤더를 위조하여
        # IP 검증을 우회할 수 있음. 실제 TCP 연결 IP(client_ip)를 우선 확인한다.
        # client_ip가 내부 IP(프록시)인 경우에만 XFF의 첫 번째 항목을 신뢰한다.
        _INTERNAL_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.",
                               "172.19.", "172.20.", "192.168.", "::1")
        if any(client_ip.startswith(p) for p in _INTERNAL_PREFIXES):
            # 신뢰된 프록시 경유 — XFF 첫 번째 항목 사용
            forwarded_for = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            effective_ip = forwarded_for or client_ip
        else:
            # 직접 연결 — XFF 무시 (스푸핑 방지)
            effective_ip = client_ip

        if not any(effective_ip.startswith(p) for p in _INTERNAL_PREFIXES):
            logger.warning("metrics endpoint blocked for external IP: %s", effective_ip)
            return PlainTextResponse(content="403 Forbidden\n", status_code=403)

    text = generate_metrics_text()
    return PlainTextResponse(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get(
    "/error-test",
    summary="[Task I-3] 오류 처리 검증",
    description=(
        "공통 exception handler 동작을 확인하기 위한 stub endpoint.\n\n"
        "query param `kind`에 따라 각 예외를 강제로 발생시킨다:\n"
        "- `not_found` → 404\n"
        "- `validation` → 400 (직접 raise)\n"
        "- `auth` → 401\n"
        "- `permission` → 403\n"
        "- `conflict` → 409\n"
        "- `internal` → 500 (예상치 못한 예외)\n"
        "- 없거나 기타 → 200 (정상 응답)\n\n"
        "⚠️ 운영 환경에서는 이 endpoint를 비활성화하거나 보호해야 한다."
    ),
    tags=["system"],
)
def error_test(
    kind: str = Query(default="", description="발생시킬 오류 종류"),
) -> SuccessResponse:
    # VULN-014: debug=True 전용 — production에서 404 반환
    if not settings.debug:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    match kind:
        case "not_found":
            raise ApiNotFoundError("Test resource was not found")
        case "validation":
            raise ApiValidationError(
                "Validation test failed",
                details=[{"field": "kind", "reason": "test value is invalid"}],
            )
        case "auth":
            raise ApiAuthenticationError()
        case "permission":
            raise ApiPermissionDeniedError()
        case "conflict":
            raise ApiConflictError("Test resource already exists")
        case "internal":
            raise RuntimeError("Simulated unexpected internal error")
        case _:
            return success_response(data={"error_test": "ok", "kind": kind or "none"})
