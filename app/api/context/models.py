"""
RequestContext — 요청 생명주기 동안 공유되는 컨텍스트 객체.

request.state.context에 저장되며 미들웨어에서 초기화된다.
actor는 anonymous로 초기화되고, auth dependency(Task I-5)가 갱신한다.

설계 원칙:
  - router / service는 request.state.context를 직접 수정하지 않는다.
  - actor 갱신은 resolve_current_actor dependency 한 곳에서만 한다.
  - request_id / trace_id는 미들웨어에서 한 번만 세팅된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.api.auth.models import ActorContext


def _make_anonymous_actor() -> "ActorContext":
    """순환 import 없이 anonymous ActorContext를 생성하는 factory."""
    from app.api.auth.models import ActorContext, ActorType

    return ActorContext(
        actor_type=ActorType.ANONYMOUS,
        actor_id=None,
        is_authenticated=False,
        auth_method=None,
        tenant_id=None,
    )


@dataclass
class RequestContext:
    """요청 단위 컨텍스트.

    Attributes:
        request_id: 요청 추적용 ID. X-Request-ID 헤더 우선, 없으면 UUID 생성.
        trace_id:   분산 추적 ID. X-Trace-ID 헤더에서 읽는다. 없으면 None.
        actor:      현재 요청의 주체. 기본값 anonymous.
    """

    request_id: str
    trace_id: Optional[str] = None
    actor: "ActorContext" = field(default_factory=_make_anonymous_actor)
