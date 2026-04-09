"""
Diff router — /api/v1/documents/{document_id}/versions

Phase 9: 변경 비교 및 이력 가시화 기능 구축

엔드포인트:
  GET /documents/{doc_id}/versions/{v_id}/diff
      — 직전 버전 대비 전체 diff

  GET /documents/{doc_id}/versions/{v1_id}/diff/{v2_id}
      — 두 버전 간 전체 diff

  GET /documents/{doc_id}/versions/{v_id}/diff/summary
      — 직전 버전 대비 변경 요약 (경량)

  GET /documents/{doc_id}/versions/{v1_id}/diff/{v2_id}/summary
      — 두 버전 간 변경 요약 (경량)

플랫폼 규약 적용:
  - success_response (envelope)
  - ApiNotFoundError / ApiValidationError (공통 오류)
  - resolve_current_actor + authorization_service (권한 검증)
  - request.state.context → request_id / trace_id
"""

from fastapi import APIRouter, Depends, Query, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.context import get_request_ids
from app.api.responses import SuccessResponse, success_response
from app.db import get_db
from app.services.diff_service import DEFAULT_MAX_INLINE_LENGTH, diff_service

router = APIRouter()


# ---------------------------------------------------------------------------
# 공통 권한 검증 헬퍼
# ---------------------------------------------------------------------------


def _authorize_diff(actor: ActorContext, document_id: str) -> None:
    """diff 요청에 대한 문서 읽기 권한 검증.

    diff는 문서의 변경 이력(이전/이후 콘텐츠 포함)을 노출하므로
    미인증(anonymous) 접근을 허용하지 않는다.
    """
    authorization_service.authorize(
        actor=actor,
        action="document.read",
        resource=ResourceRef(resource_type="document", resource_id=document_id),
        require_authenticated=True,
    )


# ---------------------------------------------------------------------------
# GET /documents/{doc_id}/versions/{v_id}/diff
# — 직전 버전 대비 전체 diff
# ---------------------------------------------------------------------------


@router.get(
    "/{v_id}/diff",
    summary="직전 버전 대비 전체 diff",
    description=(
        "지정한 버전의 직전 버전 대비 구조 단위 변경 사항을 반환한다.\n\n"
        "- `inline_diff=true` : 수정된 노드 내 텍스트 인라인 diff 포함\n"
        "- `include_unchanged=true` : 변경 없는 노드도 포함\n"
        "- 첫 번째 버전은 직전 버전이 없으므로 404 `NO_PREVIOUS_VERSION` 반환"
    ),
    response_model=SuccessResponse,
)
def get_diff_with_previous(
    document_id: str,
    v_id: str,
    request: Request,
    inline_diff: bool = Query(False, description="MODIFIED 노드 텍스트 인라인 diff 포함"),
    include_unchanged: bool = Query(False, description="UNCHANGED 노드 포함"),
    max_inline_length: int = Query(
        DEFAULT_MAX_INLINE_LENGTH,
        ge=100,
        le=50_000,
        description="인라인 diff 최대 텍스트 길이",
    ),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _authorize_diff(actor, document_id)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        result = diff_service.compute_diff_with_previous(
            conn,
            document_id=document_id,
            version_id=v_id,
            inline_diff=inline_diff,
            include_unchanged=include_unchanged,
            max_inline_length=max_inline_length,
        )

    return success_response(
        data=result.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /documents/{doc_id}/versions/{v_id}/diff/summary
# — 직전 버전 대비 변경 요약 (경량)
# ---------------------------------------------------------------------------


@router.get(
    "/{v_id}/diff/summary",
    summary="직전 버전 대비 변경 요약",
    description=(
        "지정한 버전의 직전 버전 대비 변경 요약만 반환한다 (경량 응답).\n\n"
        "워크플로 검토 화면 배너, 버전 목록 등에 사용한다."
    ),
    response_model=SuccessResponse,
)
def get_diff_summary_with_previous(
    document_id: str,
    v_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _authorize_diff(actor, document_id)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        result = diff_service.compute_summary_with_previous(
            conn,
            document_id=document_id,
            version_id=v_id,
        )

    return success_response(
        data=result.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /documents/{doc_id}/versions/{v1_id}/diff/{v2_id}
# — 두 버전 간 전체 diff
# ---------------------------------------------------------------------------


@router.get(
    "/{v1_id}/diff/{v2_id}",
    summary="두 버전 간 전체 diff",
    description=(
        "두 버전을 명시적으로 지정하여 구조 단위 변경 사항을 반환한다.\n\n"
        "- v1_id: 이전 버전 (before)\n"
        "- v2_id: 이후 버전 (after)\n"
        "- v1_id == v2_id 이면 400 `SAME_VERSION` 반환"
    ),
    response_model=SuccessResponse,
)
def get_diff_between_versions(
    document_id: str,
    v1_id: str,
    v2_id: str,
    request: Request,
    inline_diff: bool = Query(False, description="MODIFIED 노드 텍스트 인라인 diff 포함"),
    include_unchanged: bool = Query(False, description="UNCHANGED 노드 포함"),
    max_inline_length: int = Query(
        DEFAULT_MAX_INLINE_LENGTH,
        ge=100,
        le=50_000,
        description="인라인 diff 최대 텍스트 길이",
    ),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _authorize_diff(actor, document_id)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        result = diff_service.compute_diff(
            conn,
            document_id=document_id,
            version_a_id=v1_id,
            version_b_id=v2_id,
            inline_diff=inline_diff,
            include_unchanged=include_unchanged,
            max_inline_length=max_inline_length,
        )

    return success_response(
        data=result.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET /documents/{doc_id}/versions/{v1_id}/diff/{v2_id}/summary
# — 두 버전 간 변경 요약 (경량)
# ---------------------------------------------------------------------------


@router.get(
    "/{v1_id}/diff/{v2_id}/summary",
    summary="두 버전 간 변경 요약",
    description="두 버전 간 변경 요약만 반환한다 (경량 응답).",
    response_model=SuccessResponse,
)
def get_diff_summary_between_versions(
    document_id: str,
    v1_id: str,
    v2_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _authorize_diff(actor, document_id)
    request_id, trace_id = get_request_ids(request)

    with get_db() as conn:
        result = diff_service.compute_summary_only(
            conn,
            document_id=document_id,
            version_a_id=v1_id,
            version_b_id=v2_id,
        )

    return success_response(
        data=result.model_dump(),
        request_id=request_id,
        trace_id=trace_id,
    )
