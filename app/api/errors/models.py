"""
공통 오류 응답 envelope 모델.

모든 오류 응답의 최상위 계약:
  - ErrorObject  : error.code / message / details
  - ErrorResponse: { "error": {...}, "meta": {...} }

설계 원칙:
  - 최상위는 error, meta 두 가지만 (성공 응답의 data/meta와 혼용 금지)
  - error.code는 클라이언트 분기용 식별자 (snake_case)
  - error.message는 외부 노출 가능한 안전한 요약
  - error.details는 선택적 구조화 데이터 (validation field 목록 등)
  - meta.request_id / trace_id는 Task I-4에서 자동 주입 예정
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ErrorObject(BaseModel):
    """오류 본문 구조.

    code: 기계 친화적 식별자 (예: resource_not_found)
    message: 외부 노출 가능한 요약 문장
    details: validation field 목록 등 선택적 구조 데이터
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: Optional[Any] = None


class ErrorMeta(BaseModel):
    """오류 응답의 meta 정보.

    request_id, trace_id는 Task I-4 request context 구현 후 자동 주입 예정.
    지금은 수동 또는 None으로 유지한다.
    """

    model_config = ConfigDict(extra="allow")

    request_id: Optional[str] = None
    trace_id: Optional[str] = None


class ErrorResponse(BaseModel):
    """오류 응답 envelope.

    사용 예:
        ErrorResponse(
            error=ErrorObject(code="resource_not_found", message="Document not found"),
            meta=ErrorMeta(request_id="req_123"),
        )

    JSON 결과:
        {
          "error": { "code": "resource_not_found", "message": "Document not found" },
          "meta": { "request_id": "req_123", "trace_id": null }
        }
    """

    error: ErrorObject
    meta: ErrorMeta = ErrorMeta()
