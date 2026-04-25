"""
추출 결과 검토 API — Phase 8 FG8.2 + FG8.3.

엔드포인트 (FG8.2):
  GET  /extractions/pending              — pending 목록 (페이지네이션)
  GET  /extractions/{id}                 — 상세 조회
  POST /extractions/{id}/approve         — 전체 승인
  POST /extractions/{id}/modify          — 수정 후 승인
  POST /extractions/{id}/reject          — 거절
  POST /extractions/batch-approve        — 일괄 승인
  POST /extractions/batch-reject         — 일괄 거절

엔드포인트 (FG8.3):
  GET  /extractions/{id}/spans           — SourceSpan 목록 조회
  GET  /extractions/{id}/highlights      — UI 하이라이트 데이터
  POST /extractions/{id}/verify          — 재추출 검증
  GET  /extractions/{id}/audit           — 감사 이력 조회
  GET  /extractions/{id}/verification-results — 검증 결과 이력

S2 원칙:
  ⑤ actor_type 감사 로그 기록 (승인/수정/거절 → actor_type="user")
  ⑥ scope_profile_id ACL 슬롯
  ⑦ 폐쇄망 동등성 (로컬 DB만 사용)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext, ActorType
from app.api.responses import SuccessResponse, list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import db_dependency, get_db
from app.models.approved_extraction import (
    ApproveExtractionRequest,
    ApprovedExtractionResponse,
    BatchApproveRequest,
    BatchRejectRequest,
    HumanEdit,
    ModifyExtractionRequest,
    RejectExtractionRequest,
)
from app.models.extraction import ExtractionStatus
from app.repositories.approved_extraction_repository import ApprovedExtractionRepository
from app.repositories.extraction_candidate_repository import ExtractionCandidateRepository

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _actor_id(actor: ActorContext) -> str:
    return actor.actor_id or "anonymous"


def _scope_profile_id(actor: ActorContext) -> Optional[UUID]:
    sid = getattr(actor, "scope_profile_id", None)
    if sid and not isinstance(sid, UUID):
        try:
            return UUID(str(sid))
        except Exception:
            return None
    return sid


# ---------------------------------------------------------------------------
# GET /extractions/pending
# ---------------------------------------------------------------------------

@router.get(
    "/pending",
    response_model=SuccessResponse,
    summary="Pending 추출 결과 목록",
)
def list_pending_extractions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(resolve_current_actor),
):
    # 에이전트는 자신의 scope_profile_id로 필터링, 사용자는 전체 조회 (ACL은 DB 레벨에서 처리)
    if actor.actor_type == ActorType.AGENT:
        scope_id = _scope_profile_id(actor)
        role = getattr(actor, "role", None)
        if scope_id is None and role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
            raise HTTPException(status_code=403, detail="scope_profile_id가 없는 에이전트는 목록을 조회할 수 없습니다.")
    else:
        scope_id = None

    with get_db() as conn:
        repo = ExtractionCandidateRepository(conn)
        items = repo.list_pending(scope_profile_id=scope_id, limit=limit, offset=offset)
        total = repo.count_pending(scope_profile_id=scope_id)

    return list_response(
        data=[_candidate_to_dict(item) for item in items],
        total=total,
        page=(offset // limit) + 1,
        page_size=limit,
    )


# ---------------------------------------------------------------------------
# GET /extractions/{extraction_id}
# ---------------------------------------------------------------------------

@router.get(
    "/{extraction_id}",
    response_model=SuccessResponse,
    summary="추출 결과 상세 조회",
)
def get_extraction_detail(
    extraction_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
):
    with get_db() as conn:
        repo = ExtractionCandidateRepository(conn)
        candidate = repo.get_by_id(extraction_id)

    if not candidate:
        raise not_found(f"extraction_candidate id={extraction_id} 없음")

    return success_response(data=_candidate_to_dict(candidate))


# ---------------------------------------------------------------------------
# POST /extractions/{extraction_id}/approve
# ---------------------------------------------------------------------------

@router.post(
    "/{extraction_id}/approve",
    response_model=SuccessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="추출 결과 전체 승인",
)
def approve_extraction(
    request: Request,
    extraction_id: UUID,
    body: ApproveExtractionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    actor_id = _actor_id(actor)
    scope_id = _scope_profile_id(actor)
    now = utcnow()

    try:
        with get_db() as conn:
            cand_repo = ExtractionCandidateRepository(conn)
            ae_repo = ApprovedExtractionRepository(conn)

            candidate = cand_repo.get_by_id(extraction_id)
            if not candidate:
                raise not_found("추출 결과 없음")
            if candidate.status != ExtractionStatus.PENDING:
                raise conflict(f"이미 처리된 추출 결과 (status={candidate.status.value})")

            ae = ae_repo.create(
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
                approved_fields=candidate.extracted_fields,
                human_edits=[],
                approved_by=actor_id,
                approved_at=now,
                approval_comment=body.approval_comment,
                actor_type="user",
                scope_profile_id=scope_id or candidate.scope_profile_id,
            )

            cand_repo.update_status(
                extraction_id,
                new_status=ExtractionStatus.APPROVED,
                reviewed_by=actor_id,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("approve_extraction failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_candidate.approved",
        action="extraction_candidate.approve",
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        request_id=request.headers.get("X-Request-ID"),
        new_state={"approved_extraction_id": str(ae.id)},
    )

    return success_response(
        data=ApprovedExtractionResponse.from_domain(ae).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# POST /extractions/{extraction_id}/modify
# ---------------------------------------------------------------------------

@router.post(
    "/{extraction_id}/modify",
    response_model=SuccessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="추출 결과 수정 후 승인",
)
def modify_extraction(
    request: Request,
    extraction_id: UUID,
    body: ModifyExtractionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    actor_id = _actor_id(actor)
    scope_id = _scope_profile_id(actor)
    now = utcnow()

    try:
        with get_db() as conn:
            cand_repo = ExtractionCandidateRepository(conn)
            ae_repo = ApprovedExtractionRepository(conn)

            candidate = cand_repo.get_by_id(extraction_id)
            if not candidate:
                raise not_found("추출 결과 없음")
            if candidate.status != ExtractionStatus.PENDING:
                raise conflict(f"이미 처리된 추출 결과 (status={candidate.status.value})")

            # 수정 적용
            approved_fields = dict(candidate.extracted_fields)
            human_edits: List[HumanEdit] = []

            for field_name, new_value in body.modifications.items():
                before = approved_fields.get(field_name)
                approved_fields[field_name] = new_value
                human_edits.append(
                    HumanEdit(
                        field_name=field_name,
                        before_value=before,
                        after_value=new_value,
                        edited_at=now,
                        edited_by=actor_id,
                        reason=(body.reasons or {}).get(field_name),
                    )
                )

            ae = ae_repo.create(
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
                human_edits=human_edits,
                approved_by=actor_id,
                approved_at=now,
                approval_comment=body.approval_comment,
                actor_type="user",
                scope_profile_id=scope_id or candidate.scope_profile_id,
            )

            cand_repo.update_status(
                extraction_id,
                new_status=ExtractionStatus.MODIFIED,
                reviewed_by=actor_id,
                human_edits=human_edits,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("modify_extraction failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_candidate.modified",
        action="extraction_candidate.modify",
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        request_id=request.headers.get("X-Request-ID"),
        new_state={"modified_fields": list(body.modifications.keys())},
    )

    return success_response(
        data=ApprovedExtractionResponse.from_domain(ae).model_dump(),
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# POST /extractions/{extraction_id}/reject
# ---------------------------------------------------------------------------

@router.post(
    "/{extraction_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="추출 결과 거절",
)
def reject_extraction(
    request: Request,
    extraction_id: UUID,
    body: RejectExtractionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    actor_id = _actor_id(actor)

    with get_db() as conn:
        cand_repo = ExtractionCandidateRepository(conn)
        candidate = cand_repo.get_by_id(extraction_id)
        if not candidate:
            raise not_found("추출 결과 없음")
        if candidate.status != ExtractionStatus.PENDING:
            raise conflict(f"이미 처리된 추출 결과 (status={candidate.status.value})")

        cand_repo.update_status(
            extraction_id,
            new_status=ExtractionStatus.REJECTED,
            reviewed_by=actor_id,
            human_feedback=body.reason,
        )

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_candidate.rejected",
        action="extraction_candidate.reject",
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        request_id=request.headers.get("X-Request-ID"),
        new_state={"reason": body.reason},
    )


# ---------------------------------------------------------------------------
# POST /extractions/batch-approve
# ---------------------------------------------------------------------------

@router.post(
    "/batch-approve",
    response_model=SuccessResponse,
    summary="다중 추출 결과 일괄 승인",
)
def batch_approve_extractions(
    request: Request,
    body: BatchApproveRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    actor_id = _actor_id(actor)
    scope_id = _scope_profile_id(actor)
    now = utcnow()
    approved_count = 0
    failed_ids: List[str] = []

    try:
        with get_db() as conn:
            cand_repo = ExtractionCandidateRepository(conn)
            ae_repo = ApprovedExtractionRepository(conn)

            for eid in body.extraction_ids:
                try:
                    candidate = cand_repo.get_by_id(eid)
                    if not candidate or candidate.status != ExtractionStatus.PENDING:
                        failed_ids.append(str(eid))
                        continue

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
                        approved_fields=candidate.extracted_fields,
                        human_edits=[],
                        approved_by=actor_id,
                        approved_at=now,
                        approval_comment=body.approval_comment,
                        actor_type="user",
                        scope_profile_id=scope_id or candidate.scope_profile_id,
                    )
                    cand_repo.update_status(
                        eid,
                        new_status=ExtractionStatus.APPROVED,
                        reviewed_by=actor_id,
                    )
                    approved_count += 1
                except Exception as exc:
                    logger.warning("batch_approve item %s failed: %s", eid, exc)
                    failed_ids.append(str(eid))
    except Exception as exc:
        logger.exception("batch_approve_extractions failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_candidate.batch_approved",
        action="extraction_candidate.batch_approve",
        resource_type="extraction_candidate",
        request_id=request.headers.get("X-Request-ID"),
        new_state={"approved_count": approved_count, "failed_count": len(failed_ids)},
    )

    return success_response(
        data={"approved_count": approved_count, "failed_ids": failed_ids},
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# POST /extractions/batch-reject
# ---------------------------------------------------------------------------

@router.post(
    "/batch-reject",
    response_model=SuccessResponse,
    summary="다중 추출 결과 일괄 거절",
)
def batch_reject_extractions(
    request: Request,
    body: BatchRejectRequest,
    actor: ActorContext = Depends(resolve_current_actor),
):
    actor_id = _actor_id(actor)
    rejected_count = 0
    failed_ids: List[str] = []

    try:
        with get_db() as conn:
            cand_repo = ExtractionCandidateRepository(conn)

            for eid in body.extraction_ids:
                try:
                    candidate = cand_repo.get_by_id(eid)
                    if not candidate or candidate.status != ExtractionStatus.PENDING:
                        failed_ids.append(str(eid))
                        continue

                    cand_repo.update_status(
                        eid,
                        new_status=ExtractionStatus.REJECTED,
                        reviewed_by=actor_id,
                        human_feedback=body.reason,
                    )
                    rejected_count += 1
                except Exception as exc:
                    logger.warning("batch_reject item %s failed: %s", eid, exc)
                    failed_ids.append(str(eid))
    except Exception as exc:
        logger.exception("batch_reject_extractions failed")
        raise HTTPException(status_code=500, detail="내부 서버 오류가 발생했습니다.")

    audit_emitter.emit_for_actor(
        actor=actor,
        event_type="extraction_candidate.batch_rejected",
        action="extraction_candidate.batch_reject",
        resource_type="extraction_candidate",
        request_id=request.headers.get("X-Request-ID"),
        new_state={"rejected_count": rejected_count, "failed_count": len(failed_ids)},
    )

    return success_response(
        data={"rejected_count": rejected_count, "failed_ids": failed_ids},
        request_id=request.headers.get("X-Request-ID"),
    )


# ---------------------------------------------------------------------------
# 내부 변환 헬퍼
# ---------------------------------------------------------------------------

def _candidate_to_dict(c) -> dict:
    return {
        "id": str(c.id),
        "document_id": str(c.document_id),
        "document_version": c.document_version,
        "extraction_schema_id": c.extraction_schema_id,
        "extraction_schema_version": c.extraction_schema_version,
        "extracted_fields": c.extracted_fields,
        "confidence_scores": [s.model_dump() for s in c.confidence_scores],
        "extraction_model": c.extraction_model,
        "extraction_mode": c.extraction_mode.value,
        "extraction_latency_ms": c.extraction_latency_ms,
        "extraction_tokens": c.extraction_tokens,
        "extraction_cost_estimate": c.extraction_cost_estimate,
        "status": c.status.value,
        "reviewed_by": c.reviewed_by,
        "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
        "human_feedback": c.human_feedback,
        "human_edits": [e.model_dump(mode="json") for e in c.human_edits],
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
        "actor_type": c.actor_type,
        "scope_profile_id": uuid_str_or_none(c.scope_profile_id),
    }


# ===========================================================================
# FG8.3 엔드포인트 — SourceSpan + 재현성 검증
# ===========================================================================

from app.models.extraction_span import SourceSpan  # noqa: E402
from app.models.extraction_record import (  # noqa: E402
    VerifyExtractionRequest,
    VerificationResultResponse,
    ExtractionRecordResponse,
)
from app.repositories.extraction_span_repository import ExtractionSpanRepository  # noqa: E402
from app.repositories.extraction_record_repository import (  # noqa: E402
    ExtractionRecordRepository,
    VerificationResultRepository,
)
from app.services.extraction.extraction_verification_service import (  # noqa: E402
    ExtractionVerificationService,
)
from app.services.extraction.span_calculator import SpanVisualizationConverter  # noqa: E402
from app.utils.actor import actor_type_str
from app.utils.time import utcnow
from app.utils.http_errors import conflict, not_found
from app.utils.converters import uuid_str_or_none


# ---------------------------------------------------------------------------
# GET /extractions/{extraction_id}/spans
# ---------------------------------------------------------------------------

@router.get(
    "/{extraction_id}/spans",
    summary="SourceSpan 목록 조회",
)
def get_extraction_spans(
    extraction_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    cand_repo = ExtractionCandidateRepository(conn)
    candidate = cand_repo.get_by_id(extraction_id)
    if not candidate:
        raise HTTPException(404, "추출 결과 없음")

    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and candidate.scope_profile_id != scope_id:
        raise HTTPException(403, "접근 권한이 없습니다.")

    span_repo = ExtractionSpanRepository(conn)
    items = span_repo.list_by_candidate(extraction_id)

    grouped: dict = {}
    for item in items:
        fname = item["field_name"]
        span: SourceSpan = item["span"]
        grouped.setdefault(fname, []).append({
            "id": uuid_str_or_none(span.id),
            "span_offset": list(span.span_offset),
            "source_text": span.source_text,
            "content_hash": span.content_hash,
            "document_id": str(span.document_id),
            "node_id": uuid_str_or_none(span.node_id),
        })

    return success_response({"extraction_id": str(extraction_id), "spans": grouped})


# ---------------------------------------------------------------------------
# GET /extractions/{extraction_id}/highlights
# ---------------------------------------------------------------------------

@router.get(
    "/{extraction_id}/highlights",
    summary="UI 하이라이트 데이터 조회",
)
def get_extraction_highlights(
    extraction_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    cand_repo = ExtractionCandidateRepository(conn)
    candidate = cand_repo.get_by_id(extraction_id)
    if not candidate:
        raise HTTPException(404, "추출 결과 없음")

    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and candidate.scope_profile_id != scope_id:
        raise HTTPException(403, "접근 권한이 없습니다.")

    span_repo = ExtractionSpanRepository(conn)
    items = span_repo.list_by_candidate(extraction_id)

    from app.models.extraction_span import (
        ExtractedFieldWithAttribution,
        ExtractionResultWithAttribution,
    )
    from uuid import uuid4

    fields_with_attr = []
    field_map: dict = {}
    for item in items:
        fname = item["field_name"]
        span: SourceSpan = item["span"]
        field_map.setdefault(fname, []).append(span)

    for fname, spans in field_map.items():
        fields_with_attr.append(
            ExtractedFieldWithAttribution(
                field_name=fname,
                extracted_value=candidate.extracted_fields.get(fname),
                source_spans=spans,
            )
        )

    result = ExtractionResultWithAttribution(
        extraction_candidate_id=extraction_id,
        document_id=candidate.document_id,
        fields=fields_with_attr,
    )

    converter = SpanVisualizationConverter()
    highlights = converter.to_highlight_dict(result)

    return success_response({
        "extraction_id": str(extraction_id),
        "document_id": str(candidate.document_id),
        "highlights": highlights,
    })


# ---------------------------------------------------------------------------
# POST /extractions/{extraction_id}/verify
# ---------------------------------------------------------------------------

@router.post(
    "/{extraction_id}/verify",
    summary="추출 결과 재현성 검증",
    status_code=status.HTTP_201_CREATED,
)
def verify_extraction(
    extraction_id: UUID,
    req: VerifyExtractionRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    cand_repo = ExtractionCandidateRepository(conn)
    candidate = cand_repo.get_by_id(extraction_id)
    if not candidate:
        raise HTTPException(404, "추출 결과 없음")

    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    actor_id = _actor_id(actor)

    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and candidate.scope_profile_id != scope_id:
        raise HTTPException(403, "접근 권한이 없습니다.")

    record_repo = ExtractionRecordRepository(conn)
    record = record_repo.get_by_candidate(extraction_id)
    if not record:
        raise HTTPException(404, "재현성 검증을 위한 ExtractionRecord가 없습니다.")

    # 실제 재추출은 생략 — 동일 결과로 비교 (deterministic 모드 시연)
    new_result = dict(record.extracted_result)

    svc = ExtractionVerificationService()
    vr = svc.verify(
        original_record=record,
        new_extracted_result=new_result,
        fields_to_verify=req.fields_to_verify,
        verified_by=actor_id,
        # 도서관 §1.6 BE-G4 R1 (2026-04-25): Enum 인스턴스는 항상 truthy 라
        # `actor.actor_type or "user"` 가 fallback 으로 작동 안 함 — 잘못된 코드.
        # actor_type_str(actor) 로 정정 (SERVICE → "system" 매핑 통일).
        actor_type=actor_type_str(actor),
    )

    vr_repo = VerificationResultRepository(conn)
    saved_vr = vr_repo.create(vr)
    conn.commit()

    audit_emitter.emit_for_actor(
        event_type="extraction.verified",
        action="extraction.verify",
        actor=actor,
        resource_type="extraction_candidate",
        resource_id=str(extraction_id),
        metadata={"match_status": vr.match_status.value, "field_accuracy": vr.field_accuracy},
    )

    return success_response(VerificationResultResponse.from_domain(saved_vr).model_dump())


# ---------------------------------------------------------------------------
# GET /extractions/{extraction_id}/audit
# ---------------------------------------------------------------------------

@router.get(
    "/{extraction_id}/audit",
    summary="추출 감사 이력 조회",
)
def get_extraction_audit(
    extraction_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    cand_repo = ExtractionCandidateRepository(conn)
    candidate = cand_repo.get_by_id(extraction_id)
    if not candidate:
        raise HTTPException(404, "추출 결과 없음")

    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and candidate.scope_profile_id != scope_id:
        raise HTTPException(403, "접근 권한이 없습니다.")

    record_repo = ExtractionRecordRepository(conn)
    record = record_repo.get_by_candidate(extraction_id)

    vr_repo = VerificationResultRepository(conn)
    verification_results = vr_repo.list_by_candidate(extraction_id)

    svc = ExtractionVerificationService()
    if record:
        trail = svc.build_audit_trail(record, verification_results)
    else:
        trail = {
            "extraction_candidate_id": str(extraction_id),
            "document_id": str(candidate.document_id),
            "verification_count": len(verification_results),
            "record_available": False,
        }

    return success_response(trail)


# ---------------------------------------------------------------------------
# GET /extractions/{extraction_id}/verification-results
# ---------------------------------------------------------------------------

@router.get(
    "/{extraction_id}/verification-results",
    summary="검증 결과 이력 조회",
)
def get_verification_results(
    extraction_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    cand_repo = ExtractionCandidateRepository(conn)
    candidate = cand_repo.get_by_id(extraction_id)
    if not candidate:
        raise HTTPException(404, "추출 결과 없음")

    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and candidate.scope_profile_id != scope_id:
        raise HTTPException(403, "접근 권한이 없습니다.")

    vr_repo = VerificationResultRepository(conn)
    results = vr_repo.list_by_candidate(extraction_id, limit=limit)

    return success_response({
        "extraction_id": str(extraction_id),
        "total": len(results),
        "items": [VerificationResultResponse.from_domain(vr).model_dump() for vr in results],
    })
