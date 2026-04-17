"""
FG5.2 — 에이전트 제안 큐 Admin API 라우터.

엔드포인트:
  GET  /admin/proposals         — 제안 목록 (상태·에이전트·타입 필터, 페이지네이션)
  GET  /admin/proposals/stats   — 제안 통계 (전체, 승인 대기, 승인율)
  GET  /admin/proposals/{id}    — 제안 상세

  GET  /my/proposals            — 내 문서에 대한 에이전트 제안 (일반 사용자 뷰)

설계:
  - admin/* 엔드포인트는 ORG_ADMIN/SUPER_ADMIN 역할 필수
  - my/proposals는 인증된 일반 사용자도 접근 가능 (자신의 문서만 조회)
  - agent_proposals 테이블 + versions 테이블 JOIN으로 상세 정보 조회
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from fastapi import Body

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.errors.exceptions import ApiPermissionDeniedError
from app.api.responses import SuccessResponse, success_response
from app.db.connection import get_db
from app.services.agent_proposal_service import agent_proposal_service

logger = logging.getLogger(__name__)

router = APIRouter()

_ADMIN_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN"})
_ALLOWED_STATUS = frozenset({"pending", "approved", "rejected", "withdrawn"})
_ALLOWED_PROPOSAL_TYPE = frozenset({"draft", "transition"})


def _require_admin(actor: ActorContext) -> None:
    if not actor.is_authenticated or actor.role not in _ADMIN_ROLES:
        raise ApiPermissionDeniedError("관리자 권한이 필요합니다.")


def _require_authenticated(actor: ActorContext) -> None:
    if not actor.is_authenticated:
        raise ApiPermissionDeniedError("인증이 필요합니다.")


# ---------------------------------------------------------------------------
# GET /admin/proposals
# ---------------------------------------------------------------------------

@router.get(
    "/admin/proposals",
    response_model=SuccessResponse,
    summary="에이전트 제안 목록 조회 (Admin)",
)
def list_proposals(
    request: Request,
    status: Optional[str] = Query(None, description="상태 필터: pending | approved | rejected | withdrawn"),
    agent_id: Optional[str] = Query(None, description="에이전트 ID 필터"),
    proposal_type: Optional[str] = Query(None, description="제안 유형 필터: draft | transition"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    offset = (page - 1) * page_size
    filters = []
    params: list = []

    if status:
        if status not in _ALLOWED_STATUS:
            from app.api.errors.exceptions import ApiValidationError
            raise ApiValidationError(f"허용되지 않은 status 값: {status}")
        filters.append("ap.status = %s")
        params.append(status)
    if agent_id:
        filters.append("ap.agent_id = %s::uuid")
        params.append(agent_id)
    if proposal_type:
        if proposal_type not in _ALLOWED_PROPOSAL_TYPE:
            from app.api.errors.exceptions import ApiValidationError
            raise ApiValidationError(f"허용되지 않은 proposal_type 값: {proposal_type}")
        filters.append("ap.proposal_type = %s")
        params.append(proposal_type)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ap.id,
                    ap.agent_id,
                    a.name AS agent_name,
                    ap.proposal_type,
                    ap.reference_id,
                    ap.status,
                    ap.reviewed_by,
                    ap.review_notes,
                    ap.review_timestamp,
                    ap.created_at,
                    ap.updated_at,
                    v.document_id,
                    v.workflow_status,
                    d.title AS document_title,
                    v.title_snapshot
                FROM agent_proposals ap
                JOIN agents a ON a.id = ap.agent_id
                LEFT JOIN versions v ON v.id = ap.reference_id AND ap.proposal_type = 'draft'
                LEFT JOIN documents d ON d.id = v.document_id
                {where}
                ORDER BY ap.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset],
            )
            rows = cur.fetchall()

            cur.execute(
                f"SELECT COUNT(*) AS total FROM agent_proposals ap {where}",
                params,
            )
            total = cur.fetchone()["total"]

    items = [_row_to_proposal(r) for r in rows]
    return JSONResponse({
        "data": items,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        },
    })


# ---------------------------------------------------------------------------
# GET /admin/proposals/stats
# ---------------------------------------------------------------------------

@router.get(
    "/admin/proposals/stats",
    response_model=SuccessResponse,
    summary="에이전트 제안 통계 (Admin)",
)
def proposal_stats(
    request: Request,
    agent_id: Optional[str] = Query(None),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    params: list = []
    where = ""
    if agent_id:
        where = "WHERE agent_id = %s::uuid"
        params.append(agent_id)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*)                                        AS total,
                    COUNT(*) FILTER (WHERE status = 'pending')     AS pending_count,
                    COUNT(*) FILTER (WHERE status = 'approved')    AS approved_count,
                    COUNT(*) FILTER (WHERE status = 'rejected')    AS rejected_count,
                    COUNT(*) FILTER (WHERE status = 'withdrawn')   AS withdrawn_count,
                    COUNT(*) FILTER (WHERE proposal_type = 'draft')       AS draft_proposals,
                    COUNT(*) FILTER (WHERE proposal_type = 'transition')  AS transition_proposals
                FROM agent_proposals
                {where}
                """,
                params,
            )
            row = cur.fetchone()

    total = row["total"] or 0
    approved = row["approved_count"] or 0
    approval_rate = round(approved / total, 4) if total > 0 else 0.0

    return success_response({
        "total": total,
        "pending_count": row["pending_count"] or 0,
        "approved_count": approved,
        "rejected_count": row["rejected_count"] or 0,
        "withdrawn_count": row["withdrawn_count"] or 0,
        "approval_rate": approval_rate,
        "draft_proposals": row["draft_proposals"] or 0,
        "transition_proposals": row["transition_proposals"] or 0,
    })


# ---------------------------------------------------------------------------
# GET /admin/proposals/{proposal_id}
# ---------------------------------------------------------------------------

@router.get(
    "/admin/proposals/{proposal_id}",
    response_model=SuccessResponse,
    summary="에이전트 제안 상세 조회 (Admin)",
)
def get_proposal(
    proposal_id: str,
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    # S2 ⑥ + VULN-P7-003: SUPER_ADMIN은 전체, ORG_ADMIN은 자신의 조직 범위만 조회
    is_super = actor.role == "SUPER_ADMIN"
    org_filter = "AND (TRUE)" if is_super else "AND a.organization_id = %s::uuid"
    org_params: list = [] if is_super else [actor.organization_id or ""]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ap.id, ap.agent_id, a.name AS agent_name,
                    ap.proposal_type, ap.reference_id, ap.status,
                    ap.reviewed_by, ap.review_notes, ap.review_timestamp,
                    ap.created_at, ap.updated_at,
                    v.document_id, v.workflow_status, v.title_snapshot,
                    v.content_snapshot,
                    d.title AS document_title
                FROM agent_proposals ap
                JOIN agents a ON a.id = ap.agent_id
                LEFT JOIN versions v ON v.id = ap.reference_id AND ap.proposal_type = 'draft'
                LEFT JOIN documents d ON d.id = v.document_id
                WHERE ap.id = %s::uuid
                {org_filter}
                """,
                [proposal_id] + org_params,
            )
            row = cur.fetchone()

    if not row:
        from app.api.errors.exceptions import ApiNotFoundError
        raise ApiNotFoundError(f"제안 {proposal_id}을 찾을 수 없거나 접근 권한이 없습니다.")

    return success_response(_row_to_proposal(row, include_content=True))


# ---------------------------------------------------------------------------
# GET /my/proposals — 사용자 자신의 문서에 대한 에이전트 제안 뷰
# ---------------------------------------------------------------------------

@router.get(
    "/my/proposals",
    response_model=SuccessResponse,
    summary="내 문서의 에이전트 제안 목록 (사용자 뷰)",
)
def my_proposals(
    request: Request,
    status: Optional[str] = Query("pending", description="상태 필터: pending | approved | rejected | withdrawn"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_authenticated(actor)

    actor_id = actor.resolved_id
    offset = (page - 1) * page_size

    params: list = [actor_id]
    status_filter = ""
    if status:
        status_filter = "AND ap.status = %s"
        params.append(status)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    ap.id,
                    ap.agent_id,
                    a.name AS agent_name,
                    ap.proposal_type,
                    ap.reference_id,
                    ap.status,
                    ap.created_at,
                    v.document_id,
                    v.workflow_status,
                    d.title AS document_title,
                    v.title_snapshot,
                    LEFT(v.content_snapshot::text, 300) AS content_preview
                FROM agent_proposals ap
                JOIN agents a ON a.id = ap.agent_id
                LEFT JOIN versions v ON v.id = ap.reference_id AND ap.proposal_type = 'draft'
                LEFT JOIN documents d ON d.id = v.document_id
                WHERE d.created_by = %s {status_filter}
                ORDER BY ap.created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset],
            )
            rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM agent_proposals ap
                LEFT JOIN versions v ON v.id = ap.reference_id AND ap.proposal_type = 'draft'
                LEFT JOIN documents d ON d.id = v.document_id
                WHERE d.created_by = %s {status_filter}
                """,
                params,
            )
            total = cur.fetchone()["total"]

    items = [_row_to_proposal(r) for r in rows]
    return success_response({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


# ---------------------------------------------------------------------------
# POST /admin/proposals/batch-approve
# ---------------------------------------------------------------------------

@router.post(
    "/admin/proposals/batch-approve",
    response_model=SuccessResponse,
    summary="에이전트 제안 일괄 승인 (Admin)",
)
def batch_approve_proposals(
    request: Request,
    ids: list[str] = Body(..., embed=True),
    feedback: Optional[str] = Body(None, embed=True),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    if len(ids) > 200:
        from app.api.errors.exceptions import ApiConflictError
        raise ApiConflictError("한 번에 최대 200개까지 일괄 처리 가능합니다.")

    approved = 0
    failed_ids: list[str] = []
    with get_db() as conn:
        for pid in ids:
            try:
                agent_proposal_service.approve_draft(
                    conn,
                    draft_id=pid,
                    reviewer_id=actor.resolved_id,
                    reviewer_role=actor.role,
                    notes=feedback,
                )
                approved += 1
            except Exception as exc:
                logger.warning("batch_approve skip %s: %s", pid, exc)
                failed_ids.append(pid)
        conn.commit()

    return success_response({"approved": approved, "skipped": len(failed_ids)})


# ---------------------------------------------------------------------------
# POST /admin/proposals/batch-reject
# ---------------------------------------------------------------------------

@router.post(
    "/admin/proposals/batch-reject",
    response_model=SuccessResponse,
    summary="에이전트 제안 일괄 거절 (Admin)",
)
def batch_reject_proposals(
    request: Request,
    ids: list[str] = Body(..., embed=True),
    feedback: Optional[str] = Body(None, embed=True),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    if len(ids) > 200:
        from app.api.errors.exceptions import ApiConflictError
        raise ApiConflictError("한 번에 최대 200개까지 일괄 처리 가능합니다.")

    rejected = 0
    failed_ids: list[str] = []
    with get_db() as conn:
        for pid in ids:
            try:
                agent_proposal_service.reject_draft(
                    conn,
                    draft_id=pid,
                    reviewer_id=actor.resolved_id,
                    reviewer_role=actor.role,
                    reason=feedback or "",
                )
                rejected += 1
            except Exception as exc:
                logger.warning("batch_reject skip %s: %s", pid, exc)
                failed_ids.append(pid)
        conn.commit()

    return success_response({"rejected": rejected, "skipped": len(failed_ids)})


# ---------------------------------------------------------------------------
# POST /admin/proposals/batch-rollback
# ---------------------------------------------------------------------------

@router.post(
    "/admin/proposals/batch-rollback",
    response_model=SuccessResponse,
    summary="에이전트 제안 일괄 롤백 (Undo, Admin)",
)
def batch_rollback_proposals(
    request: Request,
    ids: list[str] = Body(..., embed=True),
    original_action: str = Body(..., embed=True),
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    _require_admin(actor)

    with get_db() as conn:
        result = agent_proposal_service.batch_rollback(
            conn,
            proposal_ids=ids,
            original_action=original_action,
            reviewer_id=actor.resolved_id,
            reviewer_role=actor.role,
        )
        conn.commit()

    return success_response(result)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _row_to_proposal(row: dict, *, include_content: bool = False) -> dict:
    result = {
        "id": str(row["id"]),
        "agent_id": str(row["agent_id"]),
        "agent_name": row.get("agent_name"),
        "proposal_type": row["proposal_type"],
        "reference_id": str(row["reference_id"]),
        "status": row["status"],
        "reviewed_by": row.get("reviewed_by"),
        "review_notes": row.get("review_notes"),
        "review_timestamp": row["review_timestamp"].isoformat() if row.get("review_timestamp") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "document_id": str(row["document_id"]) if row.get("document_id") else None,
        "document_title": row.get("document_title"),
        "version_title": row.get("title_snapshot"),
        "workflow_status": row.get("workflow_status"),
    }
    if include_content:
        result["content_snapshot"] = row.get("content_snapshot")
    elif row.get("content_preview"):
        result["content_preview"] = row.get("content_preview")
    return result
