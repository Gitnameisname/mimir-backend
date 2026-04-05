"""
RequestContextMiddleware — 요청마다 RequestContext를 초기화하는 미들웨어.

역할:
  - request_id 생성 (X-Request-ID 헤더 우선, 없으면 UUID)
  - trace_id 전파 (X-Trace-ID 헤더에서 읽음)
  - request.state.context = RequestContext(...) 초기화
  - 기존 핸들러 호환용 request.state.request_id / trace_id 동기화
  - 응답 헤더에 X-Request-ID 포함
  - Task I-10: request completion/failure 공통 logging baseline

actor 해석은 여기서 하지 않는다.
actor extraction은 resolve_current_actor dependency(Task I-5)에서 담당한다.

logging 역할 분담:
  - middleware: HTTP baseline (method/path/status/duration/request_id)
  - service/authz/idempotency: business meaning (action/resource/result) 보강
"""

import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.context.models import RequestContext
from app.observability.logging import log_request_completion


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        trace_id = request.headers.get("X-Trace-ID") or None

        ctx = RequestContext(request_id=request_id, trace_id=trace_id)
        request.state.context = ctx

        # 기존 handlers.py _get_request_meta 호환용
        request.state.request_id = request_id
        request.state.trace_id = trace_id

        start_time = time.monotonic()

        try:
            response = await call_next(request)
        except Exception:
            # 예외는 exception handler가 처리하므로 여기서는 failure 로그만
            duration_ms = (time.monotonic() - start_time) * 1000
            actor = getattr(ctx, "actor", None)
            log_request_completion(
                request_id=request_id,
                trace_id=trace_id,
                actor_id=getattr(actor, "actor_id", None) if actor else None,
                actor_type=getattr(actor, "actor_type", None) if actor else None,
                http_method=request.method,
                path=request.url.path,
                status_code=500,
                duration_ms=duration_ms,
                result="failure",
            )
            raise

        duration_ms = (time.monotonic() - start_time) * 1000
        status_code = response.status_code

        # actor 정보는 resolve_current_actor가 ctx.actor를 갱신한 후 읽힌다
        actor = getattr(ctx, "actor", None)
        actor_id = getattr(actor, "actor_id", None) if actor else None
        actor_type = str(getattr(actor, "actor_type", None)) if actor else None

        # result taxonomy: status code 기반 분류
        if status_code < 400:
            result = "success"
        elif status_code == 401:
            result = "denied"
        elif status_code == 403:
            result = "denied"
        elif status_code == 409:
            result = "conflict"
        elif status_code < 500:
            result = "validation_error"
        else:
            result = "failure"

        log_request_completion(
            request_id=request_id,
            trace_id=trace_id,
            actor_id=actor_id,
            actor_type=actor_type,
            http_method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=duration_ms,
            result=result,
        )

        response.headers["X-Request-ID"] = request_id
        return response
