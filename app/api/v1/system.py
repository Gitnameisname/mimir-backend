"""
System router — /api/v1/system

운영성 endpoint를 제공한다.
  - GET /api/v1/system/health        : 헬스체크 (공개)
  - GET /api/v1/system/info         : 서비스 메타 정보 (공개)
  - GET /api/v1/system/error-test   : 공통 오류 처리 검증용 stub (Task I-3)

이 router는 인증을 요구하지 않는 공개 endpoint만 포함한다.
향후 내부 전용 운영 endpoint가 필요하면 /admin 하위로 분리한다.
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.api.errors import (
    ApiAuthenticationError,
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiValidationError,
)
from app.api.responses import SuccessResponse, success_response
from app.config import settings

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
