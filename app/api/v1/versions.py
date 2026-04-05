"""
Versions router — /api/v1/versions

버전 리소스 독립 접근 경로 (canonical path).

  GET /api/v1/versions/{version_id} : 특정 버전 메타 조회

버전 생성/목록은 문서의 하위 리소스 경로에서 처리:
  GET  /api/v1/documents/{document_id}/versions
  POST /api/v1/documents/{document_id}/versions

플랫폼 규약 적용:
  - Task I-2: success_response (envelope)
  - Task I-3: ApiNotFoundError (공통 오류 체계)
  - Task I-4: request.state.context → request_id / trace_id
  - Task I-5: resolve_current_actor → actor 추출, authorization_service → authz hook
  - Task I-8: VersionsService → 실제 조회 구현
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.services.versions_service import versions_service

router = APIRouter()


def _ctx(request: Request) -> tuple[Optional[str], Optional[str]]:
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None
    return ctx.request_id, ctx.trace_id


# ---------------------------------------------------------------------------
# GET /versions/{version_id} — 버전 단건 조회
# ---------------------------------------------------------------------------


@router.get(
    "/{version_id}",
    summary="버전 단건 조회",
    description=(
        "특정 버전의 메타 정보를 반환한다.\n\n"
        "- 존재하지 않으면 404 `resource_not_found`.\n"
        "- nodes 전체는 포함하지 않음. "
        "`GET /versions/{version_id}/nodes` 로 별도 조회."
    ),
    response_model=SuccessResponse,
)
def get_version(
    version_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="version.read",
        resource=ResourceRef(resource_type="version", resource_id=version_id),
        require_authenticated=False,
    )

    request_id, trace_id = _ctx(request)

    with get_db() as conn:
        version = versions_service.get_version(conn, version_id)

    return success_response(
        data=version.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )
