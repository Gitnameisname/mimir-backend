"""
Global exception handler.

FastAPI application에 등록할 exception handler 모음.
모든 오류 경로가 공통 ErrorResponse envelope으로 수렴하도록 한다.

처리 대상:
  1. ApiError (플랫폼 비즈니스 예외)     → http_status + error_code 그대로 변환
  2. RequestValidationError              → 400 validation_error, details에 field 목록
  3. HTTPException (Starlette/FastAPI)   → 상태코드 유지, generic message 매핑
  4. Exception (예상치 못한 모든 예외)   → 500 internal_server_error, 내부 정보 숨김

안전 원칙:
  - stack trace / raw DB 오류 / 내부 클래스명은 외부 응답에 절대 포함하지 않는다.
  - 내부 상세 정보는 logger를 통해 서버 측에만 기록한다.
  - request_id / trace_id는 Task I-4 request context 구현 후 자동 주입 예정.
    현재는 request.state에서 읽되, 없으면 None으로 처리한다.
"""

import logging
import traceback

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors.exceptions import ApiError
from app.api.errors.models import ErrorMeta, ErrorObject, ErrorResponse

logger = logging.getLogger(__name__)

# HTTPException status code → (error_code, safe_message) 매핑
_HTTP_STATUS_MAP: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Bad request"),
    401: ("authentication_required", "Authentication is required"),
    403: ("permission_denied", "You do not have permission to perform this action"),
    404: ("resource_not_found", "Requested resource was not found"),
    405: ("method_not_allowed", "Method not allowed"),
    409: ("resource_conflict", "Request conflicts with the current state of the resource"),
    410: ("resource_gone", "Requested resource is no longer available"),
    422: ("validation_error", "Request validation failed"),
    429: ("rate_limit_exceeded", "Too many requests. Please try again later"),
    500: ("internal_server_error", "An unexpected error occurred"),
    502: ("bad_gateway", "Upstream service error"),
    503: ("service_unavailable", "Service is temporarily unavailable"),
}

_INTERNAL_SERVER_ERROR_CODE = "internal_server_error"
_INTERNAL_SERVER_ERROR_MSG = "An unexpected error occurred"


def _get_request_meta(request: Request) -> ErrorMeta:
    """request.state에서 request_id / trace_id를 읽는다.

    RequestContextMiddleware(Task I-4/I-5)가 등록된 경우 request.state.context에서
    우선 읽고, 미들웨어가 없는 환경(테스트 등)에서는 직접 state 속성을 fallback한다.
    """
    ctx = getattr(request.state, "context", None)
    if ctx is not None:
        return ErrorMeta(request_id=ctx.request_id, trace_id=ctx.trace_id)

    # fallback: 미들웨어 없는 환경
    request_id: str | None = getattr(request.state, "request_id", None)
    trace_id: str | None = getattr(request.state, "trace_id", None)
    return ErrorMeta(request_id=request_id, trace_id=trace_id)


def _build_response(
    status_code: int,
    error_code: str,
    message: str,
    meta: ErrorMeta,
    details=None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorObject(code=error_code, message=message, details=details),
        meta=meta,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


# ---------------------------------------------------------------------------
# Handler 1: 플랫폼 비즈니스 예외 (ApiError)
# ---------------------------------------------------------------------------


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """ApiError 계층 → ErrorResponse 변환.

    내부 detail은 로그에만 기록하고 외부 응답에는 포함하지 않는다.
    Task I-10: observability logging 연결.
    """
    from app.observability.logging import log_api_event

    meta = _get_request_meta(request)

    if exc.internal_detail:
        logger.warning(
            "ApiError [%s] %s | internal: %s | request_id=%s",
            exc.error_code,
            exc.message,
            exc.internal_detail,
            meta.request_id,
        )
    else:
        logger.info(
            "ApiError [%s] %s | request_id=%s",
            exc.error_code,
            exc.message,
            meta.request_id,
        )

    # result taxonomy 분류
    if exc.http_status == 401 or exc.http_status == 403:
        result = "denied"
    elif exc.http_status == 400:
        result = "validation_error"
    elif exc.http_status == 409:
        result = "conflict"
    else:
        result = "failure"

    log_api_event(
        event_type="api.error",
        request_id=meta.request_id,
        trace_id=meta.trace_id,
        http_method=request.method,
        path=request.url.path,
        status_code=exc.http_status,
        result=result,
        error_code=exc.error_code,
    )

    # audit emit: authz denied는 감사 이벤트 후보
    if exc.http_status in (401, 403):
        try:
            from app.audit.emitter import audit_emitter
            # VULN-023: request.state.context.actor에서 actor_id 추출
            _actor = getattr(getattr(request.state, "context", None), "actor", None)
            _actor_id = getattr(_actor, "actor_id", None) if _actor else None
            audit_emitter.emit(
                event_type="authz.denied",
                action="authz.check",
                actor_id=_actor_id,
                resource_type="unknown",
                result="denied",
                request_id=meta.request_id,
                trace_id=meta.trace_id,
            )
        except Exception:
            pass

    return _build_response(
        status_code=exc.http_status,
        error_code=exc.error_code,
        message=exc.message,
        meta=meta,
        details=exc.details,
    )


# ---------------------------------------------------------------------------
# Handler 2: FastAPI RequestValidationError (Pydantic body/query/path 검증 실패)
# ---------------------------------------------------------------------------


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI/Pydantic validation 오류 → 400 validation_error.

    FastAPI 기본값(422)을 400으로 통일하고 details에 field-level 정보를 담는다.
    """
    meta = _get_request_meta(request)

    details = []
    for error in exc.errors():
        loc = error.get("loc", [])
        # 첫 번째 요소가 'body'/'query'/'path' 등 위치 구분자이므로 제거
        field_parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
        details.append(
            {
                "field": ".".join(field_parts) if field_parts else str(loc),
                "reason": error.get("msg", "Invalid value"),
                "type": error.get("type", ""),
            }
        )

    logger.info(
        "RequestValidationError | %d field(s) failed | request_id=%s",
        len(details),
        meta.request_id,
    )

    return _build_response(
        status_code=400,
        error_code="validation_error",
        message="Request validation failed",
        meta=meta,
        details=details if details else None,
    )


# ---------------------------------------------------------------------------
# Handler 3: Starlette/FastAPI HTTPException
# ---------------------------------------------------------------------------


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """HTTPException → ErrorResponse.

    exc.detail이 문자열이면 message로 사용하되, 내부 정보 유출 위험이 없을 때만 허용.
    매핑 테이블에 없는 status는 generic 메시지를 사용한다.
    """
    meta = _get_request_meta(request)
    status_code = exc.status_code

    code, default_msg = _HTTP_STATUS_MAP.get(status_code, (_INTERNAL_SERVER_ERROR_CODE, _INTERNAL_SERVER_ERROR_MSG))

    # detail이 안전한 문자열이면 그대로 사용, 복잡한 dict/list는 default로 대체
    if isinstance(exc.detail, str) and exc.detail:
        message = exc.detail
    else:
        message = default_msg

    logger.info(
        "HTTPException %d [%s] | request_id=%s",
        status_code,
        message,
        meta.request_id,
    )

    return _build_response(
        status_code=status_code,
        error_code=code,
        message=message,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Handler 4: 예상치 못한 일반 Exception → 500
# ---------------------------------------------------------------------------


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """모든 미처리 예외를 production-safe한 500 응답으로 변환한다.

    stack trace는 절대 외부 응답에 포함하지 않고 서버 로그에만 기록한다.
    Task I-10: observability failure logging 연결.
    """
    from app.observability.logging import log_api_event

    meta = _get_request_meta(request)

    logger.error(
        "Unhandled exception | request_id=%s\n%s",
        meta.request_id,
        traceback.format_exc(),
    )

    log_api_event(
        event_type="api.unhandled_error",
        request_id=meta.request_id,
        trace_id=meta.trace_id,
        http_method=request.method,
        path=request.url.path,
        status_code=500,
        result="failure",
        error_code=_INTERNAL_SERVER_ERROR_CODE,
    )

    return _build_response(
        status_code=500,
        error_code=_INTERNAL_SERVER_ERROR_CODE,
        message=_INTERNAL_SERVER_ERROR_MSG,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# 등록 helper
# ---------------------------------------------------------------------------


def register_exception_handlers(app) -> None:  # type: ignore[type-arg]
    """FastAPI app에 모든 exception handler를 등록한다.

    main.py의 create_app()에서 호출한다.
    """
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
