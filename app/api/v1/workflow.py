"""
Workflow Action API router — /api/v1/documents/{document_id}/versions/{version_id}/workflow

Phase 5 워크플로 액션 엔드포인트:
  POST  .../workflow/submit-review   DRAFT → IN_REVIEW
  POST  .../workflow/approve         IN_REVIEW → APPROVED
  POST  .../workflow/reject          IN_REVIEW → REJECTED
  POST  .../workflow/publish         APPROVED → PUBLISHED
  POST  .../workflow/archive         PUBLISHED → ARCHIVED
  POST  .../workflow/return-to-draft REJECTED → DRAFT
  GET   .../workflow/history         워크플로 이력 조회
  GET   .../workflow/review-actions  ReviewAction 목록 조회

설계 원칙 (Task 5-4):
  - 액션별 엔드포인트: OpenAPI 명확성 + 권한 정책 분리 + 감사 추적 용이성
  - 라우터는 얇게 유지 (파싱/위임/응답만)
  - 비즈니스 로직은 WorkflowService에 위임
  - 역할(actor_role)은 ActorContext.role(DB 조회) → X-Actor-Role 헤더(개발용) 순으로 추출
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import SuccessResponse, list_response, success_response
from app.db import get_db
from app.domain.workflow.enums import WorkflowAction
from app.schemas.workflow import (
    ReviewActionItem,
    WorkflowActionRequest,
    WorkflowActionResponse,
    WorkflowHistoryItem,
)
from app.services.workflow_service import workflow_service

router = APIRouter()


def _ctx(request: Request) -> tuple[Optional[str], Optional[str]]:
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None
    return ctx.request_id, ctx.trace_id


def _actor_info(actor: ActorContext, x_actor_role: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """actor_id와 actor_role을 반환한다.

    actor_role 우선순위:
      1. ActorContext.role — API key / dev header / bearer(DB 조회) 경로에서 채워짐
      2. X-Actor-Role 헤더 — ActorContext.role이 None일 때만 사용 (개발 편의용)
      3. None — workflow_service에서 AUTHOR로 폴백
    """
    actor_id = actor.actor_id if actor.is_authenticated else None
    role = getattr(actor, "role", None) or x_actor_role
    return actor_id, role


def _history_item(h) -> dict:
    return WorkflowHistoryItem(
        id=h.id,
        document_id=h.document_id,
        version_id=h.version_id,
        from_status=h.from_status,
        to_status=h.to_status,
        action=h.action,
        actor_id=h.actor_id,
        actor_role=h.actor_role,
        comment=h.comment,
        reason=h.reason,
        created_at=h.created_at,
    ).model_dump()


def _review_item(r) -> dict:
    return ReviewActionItem(
        id=r.id,
        document_id=r.document_id,
        version_id=r.version_id,
        action_type=r.action_type,
        from_status=r.from_status,
        to_status=r.to_status,
        actor_id=r.actor_id,
        actor_role=r.actor_role,
        comment=r.comment,
        reason=r.reason,
        metadata=r.metadata,
        created_at=r.created_at,
    ).model_dump()


# ---------------------------------------------------------------------------
# POST .../workflow/submit-review — DRAFT → IN_REVIEW
# ---------------------------------------------------------------------------


@router.post(
    "/submit-review",
    status_code=200,
    summary="검토 요청 (DRAFT → IN_REVIEW)",
    description=(
        "Draft 버전을 검토 요청 상태로 전환한다.\n\n"
        "**허용 역할**: `author`, `admin`\n"
        "**전이**: `draft` → `in_review`"
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def submit_review(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(
        default=None,
        alias="X-Actor-Role",
        description="워크플로 역할 (stub 기간 테스트용: author|reviewer|approver|admin)",
    ),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.submit_review",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.submit_review(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST .../workflow/approve — IN_REVIEW → APPROVED
# ---------------------------------------------------------------------------


@router.post(
    "/approve",
    status_code=200,
    summary="승인 (IN_REVIEW → APPROVED)",
    description=(
        "검토 중인 버전을 승인 상태로 전환한다.\n\n"
        "**허용 역할**: `approver`, `admin`\n"
        "**전이**: `in_review` → `approved`"
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def approve(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(default=None, alias="X-Actor-Role"),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.approve",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.approve(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST .../workflow/reject — IN_REVIEW → REJECTED
# ---------------------------------------------------------------------------


@router.post(
    "/reject",
    status_code=200,
    summary="반려 (IN_REVIEW → REJECTED)",
    description=(
        "검토 중인 버전을 반려 상태로 전환한다.\n\n"
        "**허용 역할**: `reviewer`, `approver`, `admin`\n"
        "**전이**: `in_review` → `rejected`\n\n"
        "`reason` 입력을 강력 권장한다."
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def reject(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(default=None, alias="X-Actor-Role"),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.reject",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.reject(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST .../workflow/publish — APPROVED → PUBLISHED
# ---------------------------------------------------------------------------


@router.post(
    "/publish",
    status_code=200,
    summary="게시 (APPROVED → PUBLISHED)",
    description=(
        "승인된 버전을 공식 게시 상태로 전환한다.\n\n"
        "**허용 역할**: `approver`, `admin`\n"
        "**전이**: `approved` → `published`\n\n"
        "게시 후 해당 버전 내용은 immutable로 취급된다."
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def publish(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(default=None, alias="X-Actor-Role"),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.publish",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.publish(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST .../workflow/archive — PUBLISHED → ARCHIVED
# ---------------------------------------------------------------------------


@router.post(
    "/archive",
    status_code=200,
    summary="보관 (PUBLISHED → ARCHIVED)",
    description=(
        "게시된 버전을 보관 상태로 전환한다.\n\n"
        "**허용 역할**: `approver`, `admin`\n"
        "**전이**: `published` → `archived`\n\n"
        "보관 후에는 추가 상태 전이가 불가능하다."
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def archive(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(default=None, alias="X-Actor-Role"),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.archive",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.archive(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# POST .../workflow/return-to-draft — REJECTED → DRAFT
# ---------------------------------------------------------------------------


@router.post(
    "/return-to-draft",
    status_code=200,
    summary="Draft 복귀 (REJECTED → DRAFT)",
    description=(
        "반려된 버전을 Draft 상태로 복귀시켜 재작업을 허용한다.\n\n"
        "**허용 역할**: `author`, `admin`\n"
        "**전이**: `rejected` → `draft`"
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def return_to_draft(
    document_id: str,
    version_id: str,
    request: Request,
    body: WorkflowActionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    x_actor_role: Optional[str] = Header(default=None, alias="X-Actor-Role"),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.return_to_draft",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=True,
    )
    request_id, trace_id = _ctx(request)
    actor_id, actor_role = _actor_info(actor, x_actor_role)

    with get_db() as conn:
        result = workflow_service.return_to_draft(
            conn, document_id, version_id,
            actor_id=actor_id, actor_role=actor_role,
            comment=body.comment, reason=body.reason,
            expected_current_status=body.expected_current_status,
            request_id=request_id, trace_id=trace_id,
        )

    return success_response(
        data=WorkflowActionResponse(**result).model_dump(),
        request_id=request_id, trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# GET .../workflow/history — 워크플로 이력 조회
# ---------------------------------------------------------------------------


@router.get(
    "/history",
    summary="워크플로 이력 조회",
    description=(
        "문서 버전의 전체 워크플로 상태 전이 이력을 반환한다.\n\n"
        "- 최신 순으로 정렬\n"
        "- `limit` / `offset` 으로 페이지네이션"
    ),
    response_model=SuccessResponse,
    tags=["workflow"],
)
def get_workflow_history(
    document_id: str,
    version_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.history.read",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = _ctx(request)

    with get_db() as conn:
        history, total = workflow_service.get_history(
            conn, document_id, version_id=version_id, limit=limit, offset=offset
        )

    return list_response(
        data=[_history_item(h) for h in history],
        request_id=request_id,
        trace_id=trace_id,
        page=(offset // limit) + 1,
        page_size=limit,
        total=total,
        has_next=(offset + limit) < total,
    )


# ---------------------------------------------------------------------------
# GET .../workflow/review-actions — ReviewAction 목록 조회
# ---------------------------------------------------------------------------


@router.get(
    "/review-actions",
    summary="ReviewAction 목록 조회",
    description="특정 버전에 대한 검토/승인/반려 액션 기록을 반환한다.",
    response_model=SuccessResponse,
    tags=["workflow"],
)
def get_review_actions(
    document_id: str,
    version_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor=actor,
        action="workflow.review_actions.read",
        resource=ResourceRef(resource_type="version", resource_id=version_id, parent_id=document_id),
        require_authenticated=False,
    )
    request_id, trace_id = _ctx(request)

    with get_db() as conn:
        actions = workflow_service.get_review_actions(conn, version_id)

    return success_response(
        data=[_review_item(a) for a in actions],
        request_id=request_id,
        trace_id=trace_id,
    )
