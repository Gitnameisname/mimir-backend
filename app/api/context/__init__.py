from typing import Optional

from starlette.requests import Request

from app.api.context.middleware import RequestContextMiddleware
from app.api.context.models import RequestContext


def get_request_ids(request: Request) -> tuple[Optional[str], Optional[str]]:
    """request.state.context에서 (request_id, trace_id)를 추출한다.

    RequestContextMiddleware가 없는 테스트 환경에서도 안전하게 동작한다.
    """
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None
    return ctx.request_id, ctx.trace_id


__all__ = [
    "RequestContext",
    "RequestContextMiddleware",
    "get_request_ids",
]
