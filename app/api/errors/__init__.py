from app.api.errors.exceptions import (
    ApiAuthenticationError,
    ApiConflictError,
    ApiError,
    ApiIdempotencyError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiServiceUnavailableError,
    ApiValidationError,
)
from app.api.errors.handlers import register_exception_handlers
from app.api.errors.models import ErrorMeta, ErrorObject, ErrorResponse

__all__ = [
    # models
    "ErrorObject",
    "ErrorMeta",
    "ErrorResponse",
    # base
    "ApiError",
    # exceptions
    "ApiValidationError",
    "ApiAuthenticationError",
    "ApiPermissionDeniedError",
    "ApiNotFoundError",
    "ApiConflictError",
    "ApiIdempotencyError",
    "ApiServiceUnavailableError",
    # registration helper
    "register_exception_handlers",
]
