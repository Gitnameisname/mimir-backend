"""
FG5.1 — 에이전트 Draft 제안 API 라우터.

엔드포인트:
  POST /agents/{agent_id}/propose-draft
      에이전트가 proposed 상태 Draft를 생성.

  POST /agents/{agent_id}/propose-transition
      에이전트가 문서의 워크플로 전이를 제안.

  POST /agents/{agent_id}/proposals/{proposal_id}/withdraw
      에이전트가 자신의 proposed Draft 제안을 회수.

  POST /drafts/{draft_id}/approve
      인간 검토자가 proposed Draft를 승인 (→ approved).

  POST /drafts/{draft_id}/reject
      인간 검토자가 proposed Draft를 반려 (→ rejected).

설계 원칙:
  - 라우터는 얇게 유지: 파싱 → 권한 검사 → 서비스 위임 → 응답 직렬화만 담당.
  - 에이전트 엔드포인트는 에이전트 Principal 인증 또는 admin 권한 필요.
  - 승인/반려는 REVIEWER/APPROVER/ADMIN 역할 필요.
  - 모든 응답은 표준 SuccessResponse envelope 사용.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext, ActorType
from app.api.context import get_request_ids
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.api.responses import SuccessResponse, success_response
from app.db.connection import get_db
from app.schemas.agent_proposals import (
    ApproveDraftRequest,
    ApproveDraftResponse,
    ProposeDraftRequest,
    ProposeDraftResponse,
    ProposeTransitionRequest,
    ProposeTransitionResponse,
    RejectDraftRequest,
    RejectDraftResponse,
    WithdrawProposalRequest,
    WithdrawProposalResponse,
)
from app.services.agent_proposal_service import agent_proposal_service

logger = logging.getLogger(__name__)

router = APIRouter()

# 에이전트 작업 허용 역할 (관리자도 대리 호출 가능)
_AGENT_ALLOWED_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN"})
# 승인/반려 역할
_REVIEWER_ROLES = frozenset({"REVIEWER", "APPROVER", "ORG_ADMIN", "SUPER_ADMIN"})


def _resolve_request_id(request: Request) -> Optional[str]:
    ids = get_request_ids(request)
    return ids.request_id if ids else None


def _assert_agent_or_admin(actor: ActorContext) -> None:
    """에이전트 Principal이거나 관리자여야 한다."""
    if not actor.is_authenticated:
        raise ApiPermissionDeniedError("인증이 필요합니다.")
    if actor.actor_type == ActorType.AGENT:
        return
    if actor.role in _AGENT_ALLOWED_ROLES:
        return
    raise ApiPermissionDeniedError("에이전트 또는 관리자 권한이 필요합니다.")


def _assert_reviewer(actor: ActorContext) -> None:
    """Draft 승인/반려 권한 확인."""
    if not actor.is_authenticated:
        raise ApiPermissionDeniedError("인증이 필요합니다.")
    if actor.role not in _REVIEWER_ROLES:
        raise ApiPermissionDeniedError("REVIEWER/APPROVER/ADMIN 역할이 필요합니다.")


# ---------------------------------------------------------------------------
# POST /agents/{agent_id}/propose-draft
# ---------------------------------------------------------------------------

@router.post(
    "/agents/{agent_id}/propose-draft",
    response_model=SuccessResponse,
    summary="에이전트 Draft 제안 생성",
    description=(
        "에이전트가 proposed 상태의 Draft를 생성한다. "
        "생성된 Draft는 반드시 인간 검토 후 approved/rejected 로 전환된다."
    ),
)
def propose_draft(
    agent_id: str,
    body: ProposeDraftRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _assert_agent_or_admin(actor)

    # 에이전트 Principal인 경우 요청 경로의 agent_id와 actor.agent_id 일치 검증
    if actor.actor_type == ActorType.AGENT and actor.agent_id != agent_id:
        raise ApiPermissionDeniedError("자신의 agent_id로만 제안을 생성할 수 있습니다.")

    acting_on_behalf_of = getattr(actor, "acting_on_behalf_of", None)

    with get_db() as conn:
        result = agent_proposal_service.propose_draft(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of=acting_on_behalf_of,
            document_id=body.document_id,
            document_type_id=body.document_type_id,
            title=body.title,
            content=body.content,
            metadata=body.metadata,
            reason=body.reason,
            request_id=_resolve_request_id(request),
        )

    return success_response(ProposeDraftResponse(**result).model_dump())


# ---------------------------------------------------------------------------
# POST /agents/{agent_id}/propose-transition
# ---------------------------------------------------------------------------

@router.post(
    "/agents/{agent_id}/propose-transition",
    response_model=SuccessResponse,
    summary="에이전트 워크플로 전이 제안",
    description=(
        "에이전트가 문서의 워크플로 상태 전이를 제안한다. "
        "제안은 큐에 저장되며 인간 승인 후 실제 전이가 이루어진다."
    ),
)
def propose_transition(
    agent_id: str,
    body: ProposeTransitionRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _assert_agent_or_admin(actor)

    if actor.actor_type == ActorType.AGENT and actor.agent_id != agent_id:
        raise ApiPermissionDeniedError("자신의 agent_id로만 전이를 제안할 수 있습니다.")

    acting_on_behalf_of = getattr(actor, "acting_on_behalf_of", None)

    with get_db() as conn:
        result = agent_proposal_service.propose_transition(
            conn,
            agent_id=agent_id,
            acting_on_behalf_of=acting_on_behalf_of,
            document_id=body.document_id,
            target_state=body.target_state,
            reason=body.reason,
            approver_notes=body.approver_notes,
            request_id=_resolve_request_id(request),
        )

    return success_response(ProposeTransitionResponse(**result).model_dump())


# ---------------------------------------------------------------------------
# POST /agents/{agent_id}/proposals/{proposal_id}/withdraw
# ---------------------------------------------------------------------------

@router.post(
    "/agents/{agent_id}/proposals/{proposal_id}/withdraw",
    response_model=SuccessResponse,
    summary="에이전트 제안 회수",
    description=(
        "에이전트가 자신의 proposed Draft 제안을 회수한다. "
        "회수 후 상태는 withdrawn이며 더 이상 승인/반려할 수 없다."
    ),
)
def withdraw_proposal(
    agent_id: str,
    proposal_id: str,
    body: WithdrawProposalRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _assert_agent_or_admin(actor)

    if actor.actor_type == ActorType.AGENT and actor.agent_id != agent_id:
        raise ApiPermissionDeniedError("자신의 agent_id 제안만 회수할 수 있습니다.")

    with get_db() as conn:
        result = agent_proposal_service.withdraw_proposal(
            conn,
            agent_id=agent_id,
            proposal_id=proposal_id,
            reason=body.reason,
            request_id=_resolve_request_id(request),
        )

    return success_response(WithdrawProposalResponse(**result).model_dump())


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/approve
# ---------------------------------------------------------------------------

@router.post(
    "/drafts/{draft_id}/approve",
    response_model=SuccessResponse,
    summary="Draft 승인 (인간)",
    description=(
        "인간 검토자가 proposed 상태 Draft를 승인한다 (→ approved). "
        "REVIEWER/APPROVER/ADMIN 역할 필요."
    ),
)
def approve_draft(
    draft_id: str,
    body: ApproveDraftRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _assert_reviewer(actor)

    with get_db() as conn:
        result = agent_proposal_service.approve_draft(
            conn,
            draft_id=draft_id,
            reviewer_id=actor.resolved_id or "",
            reviewer_role=actor.role,
            notes=body.notes,
            request_id=_resolve_request_id(request),
        )

    return success_response(ApproveDraftResponse(**result).model_dump())


# ---------------------------------------------------------------------------
# POST /drafts/{draft_id}/reject
# ---------------------------------------------------------------------------

@router.post(
    "/drafts/{draft_id}/reject",
    response_model=SuccessResponse,
    summary="Draft 반려 (인간)",
    description=(
        "인간 검토자가 proposed 상태 Draft를 반려한다 (→ rejected). "
        "REVIEWER/APPROVER/ADMIN 역할 필요."
    ),
)
def reject_draft(
    draft_id: str,
    body: RejectDraftRequest,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _assert_reviewer(actor)

    with get_db() as conn:
        result = agent_proposal_service.reject_draft(
            conn,
            draft_id=draft_id,
            reviewer_id=actor.resolved_id or "",
            reviewer_role=actor.role,
            reason=body.reason,
            request_id=_resolve_request_id(request),
        )

    return success_response(RejectDraftResponse(**result).model_dump())
