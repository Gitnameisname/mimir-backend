"""Contributors 라우터 — /api/v1/documents/{document_id}/contributors — S3 Phase 3 FG 3-1.

엔드포인트:
    GET /api/v1/documents/{document_id}/contributors
        ?since=ISO8601
        &include_viewers=bool (기본 false)
        &limit_per_section=int (기본 50, 1~200 clamp)

응답 헤더:
    Cache-Control: private, max-age=15
    X-Viewers-Hidden-Reason: scope-policy   (FG 3-2 결합 후, viewers 가 정책에 의해 가려진 경우)

ACL:
    - viewer 가 문서를 못 보거나 문서가 없으면 404 (존재 유출 방지).
    - viewers 섹션 노출은 FG 3-2 의 Scope Profile `expose_viewers` 정책이 결합되면
      호출자 의사를 정책이 덮어씀. 본 FG 단계에서는 호출자 의사 그대로.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.rate_limit import limiter
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.schemas.contributors import ContributorItem, ContributorsResponse
from app.services.contributors_service import (
    DEFAULT_LIMIT_PER_SECTION,
    MAX_LIMIT_PER_SECTION,
    contributors_service,
)

router = APIRouter()

# S3 Phase 6 FG 6-1 (2026-05-18): per-IP rate limit (citations 패턴 정합).
_CONTRIBUTORS_LIMIT = "60/minute"


def _to_item(contributor) -> ContributorItem:
    return ContributorItem(
        actor_id=contributor.actor_id,
        display_name=contributor.display_name,
        actor_type=contributor.actor_type,
        last_activity_at=contributor.last_activity_at,
        role_badge=contributor.role_badge,
    )


@router.get(
    "/{document_id}/contributors",
    summary="문서 contributors 4 카테고리",
    description=(
        "문서의 작성자 / 편집자 / 승인자 / (선택) 최근 열람자를 한 번에 반환한다.\n\n"
        "- 정보 출처: documents.created_by + audit_events + workflow_history\n"
        "- viewers 는 `include_viewers=true` 일 때만 응답 키로 등장 (기본 false)\n"
        "- viewer 가 해당 문서를 못 보면 404 (존재 유출 방지)\n"
        "- since 는 ISO 8601 timestamp. viewers 카테고리는 since 미지정 시 기본 30일."
    ),
    response_model=SuccessResponse,
)
@limiter.limit(_CONTRIBUTORS_LIMIT)
def get_contributors(
    document_id: str,
    request: Request,
    response: Response,
    since: Optional[datetime] = Query(
        default=None, description="ISO 8601 timestamp. 카테고리별 occurred_at/created_at 하한"
    ),
    include_viewers: bool = Query(
        default=False, description="viewers 섹션 포함 여부. FG 3-2 정책 게이트가 강제 false 가능"
    ),
    limit_per_section: int = Query(
        default=DEFAULT_LIMIT_PER_SECTION,
        ge=1,
        le=MAX_LIMIT_PER_SECTION,
        description=f"카테고리당 최대 건수 (1~{MAX_LIMIT_PER_SECTION})",
    ),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="document.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )

    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        bundle = contributors_service.get_contributors(
            conn,
            document_id,
            viewer_actor=actor,
            since=since,
            include_viewers=include_viewers,
            limit_per_section=limit_per_section,
        )

    creator_item = _to_item(bundle.creator) if bundle.creator else None
    editors_items = [_to_item(c) for c in bundle.editors]
    approvers_items = [_to_item(c) for c in bundle.approvers]
    viewers_items: Optional[list[ContributorItem]] = (
        [_to_item(c) for c in bundle.viewers] if bundle.viewers_included else None
    )

    payload = ContributorsResponse(
        creator=creator_item,
        editors=editors_items,
        approvers=approvers_items,
        viewers=viewers_items,
    )

    # viewers 가 정책에 의해 가려진 경우 응답 dict 에서 키 제거 + 사유 헤더.
    # (FG 3-2 결합 후 의미가 강해지지만, 본 FG 에서도 include_viewers=false 면 키 자체 부재가 일관)
    data = payload.model_dump(exclude_none=False)
    if not bundle.viewers_included:
        data.pop("viewers", None)
        # 호출자가 명시적으로 include_viewers=true 였다면 사유 헤더로 통지.
        if include_viewers:
            response.headers["X-Viewers-Hidden-Reason"] = "scope-policy"

    response.headers["Cache-Control"] = "private, max-age=15"

    return success_response(
        data=data,
        request_id=request_id,
        trace_id=trace_id,
    )
