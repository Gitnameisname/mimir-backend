"""User Search 라우터 — S3 Phase 5 FG 5-3 (멘션 typeahead).

엔드포인트:
    GET /api/v1/users?q=<prefix>&limit=<1~50>
        - 인증된 사용자만 (any role ≥ VIEWER)
        - viewer 의 organization (user_org_roles JOIN) 안 사용자만 반환
        - 응답: { items: [{ user_id, display_name }], items_total, items_truncated }

**R-A4 정책 (절대 위반 금지)**:
  - viewer 의 user_id 는 ActorContext 에서만 추출. query / body 주입 차단.
  - 다른 organization 의 사용자 — display_name 의 어떤 prefix 로도 노출 0.
  - email / role / status 등 응답 미포함.
  - audit log 에 q prefix 저장 금지 (PII).

라우터 등록 위치 (`router.py`):
  - prefix `/users` — admin.py 의 `/admin/users` 와 다른 path. 일반 viewer 가 호출.

Rate limit (citations / mcp 패턴 — `app.api.rate_limit.limiter`):
  - `60/minute` per user — typeahead 가 빈번 호출 (debounce 150ms). 보호 충분.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.errors.exceptions import ApiAuthenticationError
from app.api.rate_limit import limiter
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.repositories.users_repository import UsersRepository
from app.schemas.user_search import UserSearchItem, UserSearchResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_USER_SEARCH_LIMIT = "60/minute"

# 모듈 수준 싱글턴 (다른 라우터 패턴 정합)
_users_repository = UsersRepository()


@router.get(
    "",
    summary="멘션 typeahead 사용자 검색 (viewer organization scope)",
    description=(
        "Annotation 멘션 입력 시 호출되는 사용자 검색. "
        "viewer 가 속한 organization 안의 다른 활성 사용자만 prefix 매칭으로 반환한다.\n\n"
        "**ACL**: viewer 의 user_org_roles JOIN 으로 결정. 다른 organization 의 사용자는 "
        "응답에 포함되지 않으며, 이는 `display_name` 의 어떤 prefix 로도 우회되지 않는다 (R-A4)."
    ),
    response_model=SuccessResponse,
    tags=["users", "mentions"],
)
@limiter.limit(_USER_SEARCH_LIMIT)
def search_users(
    request: Request,
    q: str = Query(
        ..., min_length=1, max_length=64, description="prefix 매칭 대상 (1~64자)"
    ),
    limit: int = Query(default=20, ge=1, le=50),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    if not actor.is_authenticated or not actor.actor_id:
        raise ApiAuthenticationError("로그인이 필요합니다")

    request_id, trace_id = get_request_ids(request)

    # q 정규화 — trim 만. NFC 는 호출자가 보낸 그대로 (일반적으로 frontend 가 brower
    # NFC normalization 후 전달). SQL injection 은 psycopg2 placeholder 가 차단.
    q_trimmed = q.strip()
    if not q_trimmed:
        return success_response(
            data=UserSearchResponse(items=[], items_total=0, items_truncated=False).model_dump(mode="json"),
            request_id=request_id,
            trace_id=trace_id,
        )

    # **viewer_user_id 는 ActorContext 에서만 추출** — query/body 주입 차단 (R-A4)
    viewer_user_id = actor.actor_id

    with get_db() as conn:
        rows = _users_repository.search_by_display_name_in_orgs(
            conn,
            viewer_user_id=viewer_user_id,
            query=q_trimmed,
            limit=limit,
        )

    items = [UserSearchItem(user_id=r["user_id"], display_name=r["display_name"]) for r in rows]
    response = UserSearchResponse(
        items=items,
        items_total=len(items),
        # LIMIT 에 정확히 걸린 경우 truncated=True (사용자에게 더 좁은 prefix 안내용)
        items_truncated=len(items) >= limit,
    )

    # **audit log 에 q 자체는 저장 금지** — PII 보호 (CONSTITUTION 제24조).
    # 길이와 결과 수만 기록.
    logger.info(
        "user_search — viewer=%s, q_length=%d, result_count=%d, truncated=%s",
        viewer_user_id, len(q_trimmed), len(items), response.items_truncated,
    )

    return success_response(
        data=response.model_dump(mode="json"),
        request_id=request_id,
        trace_id=trace_id,
    )
