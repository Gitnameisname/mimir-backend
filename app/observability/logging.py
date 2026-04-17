"""
Structured logging helper — 플랫폼 공통 observability 계층.

역할:
  - 요청/응답/오류/감사 이벤트에서 재사용할 수 있는 structured log를 남긴다.
  - 모든 계층(middleware, service, handler)이 같은 helper를 사용한다.
  - ad-hoc 문자열 로그가 각 레이어에 흩어지지 않게 한다.

안전 원칙:
  - raw auth token, session secret은 절대 로깅하지 않는다.
  - 전체 request body를 덤프하지 않는다. (payload size / fingerprint만)
  - 개인식별 정보는 actor_id (정규화된 식별자)만 기록한다.

역할 분담:
  - middleware: HTTP baseline (method/path/status/duration/request_id)
  - service/authz/idempotency: business meaning 보강 (action/resource/result)

result taxonomy:
  success / failure / denied / validation_error / conflict / replayed
"""

import logging
from typing import Any, Optional

_logger = logging.getLogger("mimir.api")


def log_api_event(
    *,
    event_type: str,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    actor_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    http_method: Optional[str] = None,
    path: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    status_code: Optional[int] = None,
    result: Optional[str] = None,
    duration_ms: Optional[float] = None,
    error_code: Optional[str] = None,
    idempotency_key_present: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """플랫폼 공통 structured API event 로그를 남긴다.

    모든 파라미터는 optional — 필요한 것만 채워서 호출한다.
    """
    record: dict[str, Any] = {"event": event_type}

    if request_id:
        record["request_id"] = request_id
    if trace_id:
        record["trace_id"] = trace_id
    if actor_id:
        record["actor_id"] = actor_id
    if actor_type:
        record["actor_type"] = actor_type
    if tenant_id:
        record["tenant_id"] = tenant_id
    if http_method:
        record["http_method"] = http_method
    if path:
        record["path"] = path
    if action:
        record["action"] = action
    if resource_type:
        record["resource_type"] = resource_type
    if resource_id:
        record["resource_id"] = resource_id
    if status_code is not None:
        record["status_code"] = status_code
    if result:
        record["result"] = result
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 2)
    if error_code:
        record["error_code"] = error_code
    if idempotency_key_present:
        record["idempotency_key_present"] = True
    if extra:
        record.update(extra)

    # result/status_code별 로그 레벨 분기
    status = record.get("status_code")
    if status and status >= 500:
        _logger.error("API_EVENT %s", record)
    elif result in ("failure", "denied") or (status and status >= 400):
        _logger.warning("API_EVENT %s", record)
    else:
        _logger.info("API_EVENT %s", record)


def log_request_completion(
    *,
    request_id: Optional[str],
    trace_id: Optional[str],
    actor_id: Optional[str],
    actor_type: Optional[str],
    http_method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    result: str = "success",
    error_code: Optional[str] = None,
) -> None:
    """요청 완료 시 HTTP baseline 로그를 남긴다.

    middleware에서 호출. 비즈니스 의미(action/resource)는 포함하지 않는다.
    """
    log_api_event(
        event_type="request.completed",
        request_id=request_id,
        trace_id=trace_id,
        actor_id=actor_id,
        actor_type=actor_type,
        http_method=http_method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        result=result,
        error_code=error_code,
    )
