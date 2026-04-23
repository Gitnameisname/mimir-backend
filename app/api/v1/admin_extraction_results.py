"""
Admin 추출 결과 검토 큐 라우터 — Phase 8 FG8.2/8.3 (B 스코프, 2026-04-22).

경로: `/api/v1/admin/extraction-results`

책임:
  - `/admin/extraction-queue` UI 의 전용 백엔드 계약.
  - 기존 `/api/v1/extractions/*`(에이전트/사용자 혼용) 와 분리 —
    관리자 RBAC + 풍부한 응답(documents JOIN) 제공.
  - 내부 4-상태(pending|approved|rejected|modified) ↔ 외부 3-상태
    (pending_review|approved|rejected) 어댑터.

엔드포인트:
  - GET  /admin/extraction-results                      — 목록 (필터+페이지)
  - GET  /admin/extraction-results/{id}                  — 상세
  - POST /admin/extraction-results/{id}/approve          — 승인 (overrides 선택)
  - POST /admin/extraction-results/{id}/reject           — 반려

S2 원칙:
  ① DocumentType 하드코딩 금지 — document_type 은 쿼리 파라미터, 서버는
     extraction_candidates.extraction_schema_id 로 1:1 매칭 (코드 내 비교 없음).
  ⑤ actor_type 감사 로그 — audit_emitter.emit_for_actor 로 기록.
  ⑥ scope_profile_id — 모든 조회/변경 경로에 전달.
  ⑦ 폐쇄망 — 외부 SaaS 의존 없음.

403/404/409/422 매핑:
  - 403: 관리자 권한 없음
  - 404: candidate 미존재 or soft-deleted
  - 409: 이미 처리된 상태(approve/reject 를 두 번 호출한 경우)
  - 422: 입력 검증 실패 (scope_profile_id UUID 오류 / document_type 포맷 등)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.auth.authorization import ResourceRef, authorization_service
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.models.approved_extraction import HumanEdit
from app.models.extraction import ExtractionStatus, HumanEditRecord
from app.repositories.approved_extraction_repository import (
    ApprovedExtractionRepository,
)
from app.repositories.extraction_candidate_repository import (
    ExtractionCandidateRepository,
)
from app.schemas.admin_extraction_results import (
    AdminApproveExtractionRequest,
    AdminRejectExtractionRequest,
    ExtractionResultDetail,
    ExtractionResultSummary,
    map_status_to_external,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 입력 정규화 / 권한
# ---------------------------------------------------------------------------

# document_type 쿼리 파라미터 regex — extraction_schemas P7-2 와 동일 규칙.
# (대문자 시작, 영숫자/하이픈/언더스코어만). 소문자 입력은 서버가 대문자 변환.
_DOC_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")

_VALID_EXTERNAL_STATUS = {"pending_review", "approved", "rejected"}

_PREVIEW_MAX = 2000


def _require_admin(
    request: Request,
    actor: ActorContext = Depends(resolve_current_actor),
) -> ActorContext:
    """관리자 RBAC 가드.

    GET  → admin.read (ORG_ADMIN 이상)
    POST → admin.write (SUPER_ADMIN 전용)

    `require_authenticated=True` — 미인증은 401, 권한 미달은 403 으로 응답.
    admin 라우터 전역 규약과 정확히 동일한 규칙이다(admin.py 와 중복하지 않는 이유:
    본 라우터를 admin.router 에 include 하지 않고 독립 파일로 관리하기 때문).
    """
    action = (
        "admin.write"
        if request.method in ("POST", "PATCH", "DELETE", "PUT")
        else "admin.read"
    )
    authorization_service.authorize(
        actor=actor,
        action=action,
        resource=ResourceRef(resource_type="admin"),
        require_authenticated=True,
    )
    return actor


def _parse_scope(raw: Optional[str]) -> Optional[UUID]:
    if raw is None or raw == "":
        return None
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="scope_profile_id 가 유효한 UUID 가 아닙니다.",
        )


def _normalize_document_type(raw: Optional[str]) -> Optional[str]:
    if raw is None or raw == "":
        return None
    value = raw.strip().upper()
    if not _DOC_TYPE_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=(
                f"document_type (='{raw}') 가 형식에 맞지 않습니다. "
                "영문자로 시작해 영문/숫자/하이픈/언더스코어만 허용됩니다."
            ),
        )
    return value


def _parse_status_filter(raw: Optional[str]) -> Optional[List[ExtractionStatus]]:
    """외부 status 파라미터 → 내부 ExtractionStatus 리스트.

    - None/"" → 전체 상태 (필터 없음)
    - pending_review → [PENDING]
    - approved       → [APPROVED, MODIFIED]  (내부 두 상태를 하나로 노출)
    - rejected       → [REJECTED]
    - 그 외 값       → 422
    """
    if raw is None or raw == "":
        return None
    if raw not in _VALID_EXTERNAL_STATUS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"status='{raw}' 는 허용되지 않습니다. "
                f"허용값: {sorted(_VALID_EXTERNAL_STATUS)}"
            ),
        )
    if raw == "pending_review":
        return [ExtractionStatus.PENDING]
    if raw == "approved":
        return [ExtractionStatus.APPROVED, ExtractionStatus.MODIFIED]
    return [ExtractionStatus.REJECTED]


def _row_to_summary_payload(row: dict) -> dict:
    """repository row → ExtractionResultSummary 직렬화 dict.

    document 가 삭제되었거나 처음부터 존재하지 않을 때 title 을 '(삭제된 문서)'
    로 치환(프론트가 링크 자체는 유지하되 상태를 인지 가능하게).
    """
    internal_status = row.get("status") or "pending"
    return ExtractionResultSummary(
        id=row["id"],
        document_id=row["document_id"],
        document_title=row.get("document_title") or "(삭제된 문서)",
        document_type_code=row["extraction_schema_id"],
        extracted_at=row["created_at"],
        status=map_status_to_external(internal_status),
        reviewer_id=row.get("reviewed_by"),
        reviewed_at=row.get("reviewed_at"),
        was_modified=(internal_status == "modified"),
    ).model_dump(mode="json")


def _row_to_detail_payload(row: dict) -> dict:
    """repository row → ExtractionResultDetail 직렬화 dict."""
    internal_status = row.get("status") or "pending"
    summary = row.get("document_summary") or ""
    if len(summary) > _PREVIEW_MAX:
        summary = summary[:_PREVIEW_MAX]

    extracted_fields = row.get("extracted_fields") or {}
    if isinstance(extracted_fields, str):
        import json
        try:
            extracted_fields = json.loads(extracted_fields)
        except json.JSONDecodeError:
            extracted_fields = {}

    return ExtractionResultDetail(
        id=row["id"],
        document_id=row["document_id"],
        document_title=row.get("document_title") or "(삭제된 문서)",
        document_type_code=row["extraction_schema_id"],
        extracted_at=row["created_at"],
        status=map_status_to_external(internal_status),
        reviewer_id=row.get("reviewed_by"),
        reviewed_at=row.get("reviewed_at"),
        was_modified=(internal_status == "modified"),
        original_content_preview=summary,
        extracted_fields=extracted_fields,
        field_spans={},
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /admin/extraction-results
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=SuccessResponse,
    summary="추출 결과 검토 큐 목록 조회",
)
def list_extraction_results(
    request: Request,
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    document_type: Optional[str] = Query(default=None),
    scope_profile_id: Optional[str] = Query(default=None),
    actor: ActorContext = Depends(_require_admin),
):
    statuses = _parse_status_filter(status_filter)
    doc_type = _normalize_document_type(document_type)
    scope_uuid = _parse_scope(scope_profile_id)

    offset = (page - 1) * page_size

    with get_db() as conn:
        repo = ExtractionCandidateRepository(conn)
        rows, total = repo.list_for_admin_queue(
            statuses=statuses,
            document_type=doc_type,
            scope_profile_id=scope_uuid,
            limit=page_size,
            offset=offset,
        )

    data = [_row_to_summary_payload(r) for r in rows]
    has_next = (page * page_size) < total

    return list_response(
        data=data,
        total=total,
        page=page,
        page_size=page_size,
        has_next=has_next,
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# GET /admin/extraction-results/{extraction_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{extraction_id}",
    response_model=SuccessResponse,
    summary="추출 결과 상세 조회",
)
def get_extraction_result(
    request: Request,
    extraction_id: UUID,
    scope_profile_id: Optional[str] = Query(default=None),
    actor: ActorContext = Depends(_require_admin),
):
    scope_uuid = _parse_scope(scope_profile_id)

    with get_db() as conn:
        repo = ExtractionCandidateRepository(conn)
        row = repo.get_for_admin_detail(extraction_id)

    if not row:
        raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다.")

    # scope 가 제공된 경우 교차 열람 차단.
    if scope_uuid is not None:
        candidate_scope = row.get("scope_profile_id")
        if candidate_scope is not None and UUID(str(candidate_scope)) != scope_uuid:
            # 존재 여부 노출 회피 — 다른 scope 의 리소스는 404 로 응답.
            raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다.")

    return success_response(
        data=_row_to_detail_payload(row),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# POST /admin/extraction-results/{extraction_id}/approve
# ---------------------------------------------------------------------------


@router.post(
    "/{extraction_id}/approve",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="추출 결과 승인",
)
def approve_extraction_result(
    request: Request,
    extraction_id: UUID,
    body: AdminApproveExtractionRequest,
    actor: ActorContext = Depends(_require_admin),
):
    actor_id = actor.actor_id or "anonymous"
    overrides = body.overrides or {}
    now = datetime.now(timezone.utc)

    with get_db() as conn:
        cand_repo = ExtractionCandidateRepository(conn)
        ae_repo = ApprovedExtractionRepository(conn)

        candidate = cand_repo.get_by_id(extraction_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다.")
        if candidate.status != ExtractionStatus.PENDING:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"이미 처리된 추출 결과입니다 (현재 상태: {candidate.status.value})."
                ),
            )

        # overrides 가 비어 있으면 원본 추출 필드를 그대로 승인(approve 경로).
        # overrides 가 있으면 필드별 덮어쓰기 + human_edits 기록(modify 경로).
        if overrides:
            approved_fields = dict(candidate.extracted_fields)
            human_edits: list[HumanEdit] = []
            for field_name, new_value in overrides.items():
                before = approved_fields.get(field_name)
                approved_fields[field_name] = new_value
                human_edits.append(
                    HumanEdit(
                        field_name=field_name,
                        before_value=before,
                        after_value=new_value,
                        edited_at=now,
                        edited_by=actor_id,
                        reason=None,
                    )
                )
            new_status = ExtractionStatus.MODIFIED
        else:
            approved_fields = candidate.extracted_fields
            human_edits = []
            new_status = ExtractionStatus.APPROVED

        try:
            ae_repo.create(
                candidate_id=candidate.id,
                document_id=candidate.document_id,
                document_version=candidate.document_version,
                extraction_schema_id=candidate.extraction_schema_id,
                extraction_schema_version=candidate.extraction_schema_version,
                extraction_model=candidate.extraction_model,
                extraction_latency_ms=candidate.extraction_latency_ms,
                extraction_tokens=candidate.extraction_tokens,
                extraction_cost_estimate=candidate.extraction_cost_estimate,
                extraction_prompt_version=candidate.extraction_prompt_version,
                approved_fields=approved_fields,
                human_edits=[
                    # HumanEdit(datetime) → approved_extractions 에 편의 변환
                    e for e in human_edits
                ],
                approved_by=actor_id,
                approved_at=now,
                approval_comment=body.approval_comment,
                actor_type=(
                    actor.actor_type.value
                    if hasattr(actor.actor_type, "value")
                    else str(actor.actor_type)
                ),
                scope_profile_id=candidate.scope_profile_id,
            )
        except Exception:
            logger.exception("approved_extraction 생성 실패")
            raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

        # 캔디데이트 테이블에도 human_edits 기록(감사 일관성).
        # approved_extraction 과 동일한 원본이지만 스키마 타입이 다르므로 변환.
        candidate_human_edits: list[HumanEditRecord] = [
            HumanEditRecord(
                field_name=e.field_name,
                before_value=e.before_value,
                after_value=e.after_value,
                edited_at=e.edited_at,
                edited_by=e.edited_by,
                reason=e.reason,
            )
            for e in human_edits
        ]

        updated = cand_repo.update_status(
            extraction_id,
            new_status=new_status,
            reviewed_by=actor_id,
            human_edits=candidate_human_edits or None,
        )

    if not updated:
        # 동시성으로 인해 update 가 race 했을 때. 캔디데이트가 일관되게
        # 변경되지 못했음을 409 로 통보.
        raise HTTPException(
            status_code=409,
            detail="추출 결과 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="admin.extraction_result.approved",
        action="admin.extraction_result.approve",
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        request_id=request.headers.get("X-Request-ID"),
        previous_state="pending",
        new_state=updated.status.value,
        metadata={
            "modified": bool(overrides),
            "override_field_count": len(overrides),
            "document_id": str(candidate.document_id),
            "document_type_code": candidate.extraction_schema_id,
            "scope_profile_id": (
                str(candidate.scope_profile_id) if candidate.scope_profile_id else None
            ),
        },
    )

    # 상세 포맷으로 재조회 — 클라이언트가 곧바로 캐시에 투입 가능하도록.
    with get_db() as conn:
        detail_row = ExtractionCandidateRepository(conn).get_for_admin_detail(extraction_id)

    if not detail_row:
        # 변경 직후 조회 실패는 이례적이므로 얇은 응답 유지.
        return success_response(
            data={"id": str(extraction_id), "status": map_status_to_external(updated.status.value)},
            request_id=request.headers.get("X-Request-ID"),
        )

    return success_response(
        data=_row_to_detail_payload(detail_row),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# POST /admin/extraction-results/{extraction_id}/reject
# ---------------------------------------------------------------------------


@router.post(
    "/{extraction_id}/reject",
    response_model=SuccessResponse,
    status_code=status.HTTP_200_OK,
    summary="추출 결과 반려",
)
def reject_extraction_result(
    request: Request,
    extraction_id: UUID,
    body: AdminRejectExtractionRequest,
    actor: ActorContext = Depends(_require_admin),
):
    actor_id = actor.actor_id or "anonymous"

    with get_db() as conn:
        cand_repo = ExtractionCandidateRepository(conn)
        candidate = cand_repo.get_by_id(extraction_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다.")
        if candidate.status != ExtractionStatus.PENDING:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"이미 처리된 추출 결과입니다 (현재 상태: {candidate.status.value})."
                ),
            )

        updated = cand_repo.update_status(
            extraction_id,
            new_status=ExtractionStatus.REJECTED,
            reviewed_by=actor_id,
            human_feedback=body.reason,
        )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="추출 결과 상태가 변경되었습니다. 다시 시도해주세요.",
        )

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="admin.extraction_result.rejected",
        action="admin.extraction_result.reject",
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        request_id=request.headers.get("X-Request-ID"),
        previous_state="pending",
        new_state="rejected",
        metadata={
            "reason_present": body.reason is not None,
            "document_id": str(candidate.document_id),
            "document_type_code": candidate.extraction_schema_id,
            "scope_profile_id": (
                str(candidate.scope_profile_id) if candidate.scope_profile_id else None
            ),
        },
    )

    with get_db() as conn:
        detail_row = ExtractionCandidateRepository(conn).get_for_admin_detail(extraction_id)

    if not detail_row:
        return success_response(
            data={"id": str(extraction_id), "status": "rejected"},
            request_id=request.headers.get("X-Request-ID"),
        )

    return success_response(
        data=_row_to_detail_payload(detail_row),
        request_id=request.headers.get("X-Request-ID"),
    )
