"""
플랫폼 비즈니스 예외 계층.

설계 원칙:
  - ApiError: 모든 플랫폼 예외의 베이스. HTTP status + error_code + message + details를 담는다.
  - 라우터/서비스는 HTTPException 대신 이 계층을 raise한다.
  - global handler(handlers.py)가 ApiError를 ErrorResponse로 일관되게 변환한다.
  - safe_message: 외부에 노출할 메시지 (기본값). 내부 디버그 정보는 로그에만 기록한다.

예외 계층:
  ApiError (base)
  ├── ApiValidationError       400  validation_error
  ├── ApiAuthenticationError   401  authentication_required
  ├── ApiPermissionDeniedError 403  permission_denied
  ├── ApiNotFoundError         404  resource_not_found
  ├── ApiConflictError         409  resource_conflict
  ├── ApiIdempotencyError      409  idempotency_key_conflict
  └── ApiServiceUnavailableError 503 service_unavailable  (optional)

HTTP status mapping:
  400 → validation / bad request / unsupported operation
  401 → authentication required
  403 → permission denied / authorization failure
  404 → resource not found
  409 → conflict / idempotency mismatch
  422 → 사용하지 않음 (FastAPI 기본 validation에만 허용, 플랫폼 예외는 400으로 통일)
  500 → unexpected internal error
  503 → service unavailable
"""

from typing import Any, Optional


class ApiError(Exception):
    """플랫폼 비즈니스 예외 베이스 클래스.

    Attributes:
        http_status: HTTP 응답 상태 코드
        error_code: error.code에 실릴 식별자 (snake_case)
        message: 외부 노출 가능한 요약 메시지
        details: 선택적 구조화 데이터 (validation 필드 목록 등)
        internal_detail: 내부 로그 전용 추가 정보 (외부 응답에 절대 포함하지 않음)
    """

    http_status: int = 500
    error_code: str = "internal_server_error"
    default_message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        details: Optional[Any] = None,
        internal_detail: Optional[str] = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details
        self.internal_detail = internal_detail
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# 400 — Validation / Bad Request
# ---------------------------------------------------------------------------


class ApiValidationError(ApiError):
    """요청 파라미터/바디가 유효하지 않을 때.

    details에 field-level 정보를 담을 수 있다:
        [{"field": "page_size", "reason": "must be <= 100"}]
    """

    http_status = 400
    error_code = "validation_error"
    default_message = "Request validation failed"


# ---------------------------------------------------------------------------
# 401 — Authentication
# ---------------------------------------------------------------------------


class ApiAuthenticationError(ApiError):
    """인증 정보가 없거나 유효하지 않을 때."""

    http_status = 401
    error_code = "authentication_required"
    default_message = "Authentication is required"


# ---------------------------------------------------------------------------
# 403 — Permission / Authorization
# ---------------------------------------------------------------------------


class ApiPermissionDeniedError(ApiError):
    """인증은 됐지만 해당 리소스/동작에 대한 권한이 없을 때.

    주의: 민감한 내부 권한 판단 로직은 message/details에 포함하지 말 것.
    """

    http_status = 403
    error_code = "permission_denied"
    default_message = "You do not have permission to perform this action"


# ---------------------------------------------------------------------------
# 404 — Not Found
# ---------------------------------------------------------------------------


class ApiNotFoundError(ApiError):
    """요청한 리소스가 존재하지 않을 때."""

    http_status = 404
    error_code = "resource_not_found"
    default_message = "Requested resource was not found"


# ---------------------------------------------------------------------------
# 409 — Conflict
# ---------------------------------------------------------------------------


class ApiConflictError(ApiError):
    """리소스 상태와 충돌하는 요청일 때 (예: 중복 생성, 상태 불일치)."""

    http_status = 409
    error_code = "resource_conflict"
    default_message = "Request conflicts with the current state of the resource"


class ApiIdempotencyError(ApiError):
    """Idempotency key 충돌 또는 재사용 불일치가 발생했을 때.

    TODO: Task I-9에서 idempotency 전체 구현 시 details 구조 확장 예정.
    """

    http_status = 409
    error_code = "idempotency_key_conflict"
    default_message = "Idempotency key conflict detected"


# ---------------------------------------------------------------------------
# 503 — Service Unavailable (optional)
# ---------------------------------------------------------------------------


class ApiServiceUnavailableError(ApiError):
    """의존 서비스가 일시적으로 사용 불가한 경우."""

    http_status = 503
    error_code = "service_unavailable"
    default_message = "Service is temporarily unavailable"
