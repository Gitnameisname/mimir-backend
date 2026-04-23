"""
추출 품질 평가 API — Phase 8 FG8.3 (task8-10).

엔드포인트:
  POST /extraction-evaluations/run              — 평가 실행
  GET  /extraction-evaluations/{eval_id}        — 평가 결과 조회
  GET  /extraction-evaluations/{eval_id}/compare — 평가 비교 (A/B)
  POST /extraction-evaluations/quality-gate-check — 품질 게이트 통과 여부

S2 원칙:
  ⑤ actor_type 감사 로그
  ⑥ scope_profile_id ACL
  ⑦ 폐쇄망 동등성
"""
from __future__ import annotations

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import db_dependency
from app.models.extraction_evaluation import (
    GoldenExtractionItem,
    GoldenExtractionSet,
    QualityGateCheckRequest,
    RunEvaluationRequest,
)
from app.repositories.extraction_candidate_repository import ExtractionCandidateRepository
from app.repositories.extraction_evaluation_repository import (
    ExtractionEvaluationRepository,
    GoldenExtractionItemRepository,
    GoldenExtractionSetRepository,
)
from app.services.extraction.extraction_evaluator import (
    EvaluationReportGenerator,
    ExtractionEvaluator,
    QualityGateChecker,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_WRITE_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN", "AUTHOR", "EVALUATOR"})


def _actor_id(actor: ActorContext) -> str:
    return str(actor.actor_id) if actor.actor_id else "anonymous"


def _scope_id(actor: ActorContext) -> Optional[UUID]:
    sid = getattr(actor, "scope_profile_id", None)
    if sid and not isinstance(sid, UUID):
        try:
            return UUID(str(sid))
        except Exception:
            return None
    return sid


# ---------------------------------------------------------------------------
# POST /extraction-evaluations/run
# ---------------------------------------------------------------------------

@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="추출 품질 평가 실행",
)
def run_evaluation(
    req: RunEvaluationRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    """Golden Set 기준 추출 품질을 평가한다."""
    if getattr(actor, "role", None) not in _WRITE_ROLES:
        raise HTTPException(403, "평가 실행 권한이 없습니다.")

    scope = _scope_id(actor)
    actor_id = _actor_id(actor)

    golden_repo = GoldenExtractionSetRepository(conn)
    gset = golden_repo.get_by_id(req.golden_set_id)
    if not gset:
        raise HTTPException(404, "Golden Set을 찾을 수 없습니다.")

    # scope ACL
    if getattr(actor, "role", None) not in {"ORG_ADMIN", "SUPER_ADMIN"}:
        if gset.scope_profile_id and gset.scope_profile_id != scope:
            raise HTTPException(403, "이 Golden Set에 접근할 권한이 없습니다.")

    item_repo = GoldenExtractionItemRepository(conn)
    items = item_repo.list_by_set(req.golden_set_id)
    if not items:
        raise HTTPException(422, "Golden Set에 평가 항목이 없습니다.")

    cand_repo = ExtractionCandidateRepository(conn)
    evaluator = ExtractionEvaluator()
    eval_repo = ExtractionEvaluationRepository(conn)

    saved_results = []
    for item in items:
        # 문서별 최신 extraction candidate 조회
        candidates = cand_repo.list_by_document(
            document_id=item.document_id,
            document_version=item.document_version,
            limit=1,
            offset=0,
        )
        actual_fields: dict = candidates[0].extracted_fields if candidates else {}
        candidate_id = candidates[0].id if candidates else None

        eval_result = evaluator.evaluate_extraction(
            golden_item=item,
            actual_fields=actual_fields,
            extraction_candidate_id=candidate_id,
            actor_type=actor.actor_type or "user",
            scope_profile_id=scope,
        )
        saved = eval_repo.create(eval_result)
        conn.commit()
        saved_results.append(saved)

    audit_emitter.emit_for_actor(
        event_type="extraction.evaluation.run",
        action="extraction.evaluation.run",
        actor=actor,
        resource_type="golden_extraction_set",
        resource_id=str(req.golden_set_id),
        metadata={"item_count": len(items), "result_count": len(saved_results)},
    )

    return success_response({
        "golden_set_id": str(req.golden_set_id),
        "evaluated_count": len(saved_results),
        "evaluation_ids": [str(r.id) for r in saved_results if r.id],
    })


# ---------------------------------------------------------------------------
# GET /extraction-evaluations/{eval_id}
# ---------------------------------------------------------------------------

@router.get(
    "/{eval_id}",
    summary="평가 결과 조회",
)
def get_evaluation(
    eval_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    repo = ExtractionEvaluationRepository(conn)
    result = repo.get_by_id(eval_id)
    if not result:
        raise HTTPException(404, "평가 결과를 찾을 수 없습니다.")

    scope = _scope_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
        if result.scope_profile_id and result.scope_profile_id != scope:
            raise HTTPException(403, "이 평가 결과에 접근할 권한이 없습니다.")

    return success_response(result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# GET /extraction-evaluations/{eval_id}/compare
# ---------------------------------------------------------------------------

@router.get(
    "/{eval_id}/compare",
    summary="평가 결과 비교 (A/B)",
)
def compare_evaluations(
    eval_id: UUID,
    compare_with: UUID = Query(..., description="비교 대상 evaluation ID"),
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    repo = ExtractionEvaluationRepository(conn)
    eval_a = repo.get_by_id(eval_id)
    eval_b = repo.get_by_id(compare_with)

    if not eval_a or not eval_b:
        raise HTTPException(404, "평가 결과를 찾을 수 없습니다.")

    scope = _scope_id(actor)
    role = getattr(actor, "role", None)
    for ev in [eval_a, eval_b]:
        if role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
            if ev.scope_profile_id and ev.scope_profile_id != scope:
                raise HTTPException(403, "접근 권한이 없습니다.")

    def _delta(a_val: float, b_val: float) -> dict:
        return {
            "a": round(a_val, 4),
            "b": round(b_val, 4),
            "delta": round(b_val - a_val, 4),
            "improved": b_val > a_val,
        }

    comparison = {
        "eval_a_id": str(eval_id),
        "eval_b_id": str(compare_with),
        "field_accuracy": _delta(eval_a.metrics.field_accuracy, eval_b.metrics.field_accuracy),
        "span_accuracy": _delta(eval_a.metrics.span_accuracy, eval_b.metrics.span_accuracy),
        "required_field_coverage": _delta(
            eval_a.metrics.required_field_coverage, eval_b.metrics.required_field_coverage
        ),
        "type_correctness": _delta(eval_a.metrics.type_correctness, eval_b.metrics.type_correctness),
        "overall_score": _delta(eval_a.metrics.overall_score, eval_b.metrics.overall_score),
    }

    return success_response(comparison)


# ---------------------------------------------------------------------------
# POST /extraction-evaluations/quality-gate-check
# ---------------------------------------------------------------------------

@router.post(
    "/quality-gate-check",
    summary="품질 게이트 통과 여부 확인",
)
def quality_gate_check(
    req: QualityGateCheckRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    repo = ExtractionEvaluationRepository(conn)
    result = repo.get_by_id(req.evaluation_id)
    if not result:
        raise HTTPException(404, "평가 결과를 찾을 수 없습니다.")

    scope = _scope_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
        if result.scope_profile_id and result.scope_profile_id != scope:
            raise HTTPException(403, "접근 권한이 없습니다.")

    checker = QualityGateChecker()
    gate_result = checker.check(
        result,
        field_accuracy_threshold=req.field_accuracy_threshold,
        span_accuracy_threshold=req.span_accuracy_threshold,
        required_field_coverage_threshold=req.required_field_coverage_threshold,
        type_correctness_threshold=req.type_correctness_threshold,
        overall_score_threshold=req.overall_score_threshold,
    )

    audit_emitter.emit_for_actor(
        event_type="extraction.quality_gate.checked",
        action="extraction.quality_gate.check",
        actor=actor,
        resource_type="extraction_evaluation",
        resource_id=str(req.evaluation_id),
        metadata={"passed": gate_result.passed, "failures": gate_result.failures},
    )

    return success_response(gate_result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# POST /extraction-evaluations/golden-sets  (Golden Set CRUD)
# ---------------------------------------------------------------------------

@router.post(
    "/golden-sets",
    status_code=status.HTTP_201_CREATED,
    summary="Golden Set 생성",
)
def create_golden_set(
    gset: GoldenExtractionSet,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    if getattr(actor, "role", None) not in _WRITE_ROLES:
        raise HTTPException(403, "Golden Set 생성 권한이 없습니다.")

    gset.created_by = _actor_id(actor)
    gset.actor_type = actor.actor_type or "user"
    gset.scope_profile_id = _scope_id(actor)

    repo = GoldenExtractionSetRepository(conn)
    saved = repo.create(gset)
    conn.commit()

    audit_emitter.emit_for_actor(
        event_type="extraction.golden_set.created",
        action="extraction.golden_set.create",
        actor=actor,
        resource_type="golden_extraction_set",
        resource_id=str(saved.id),
        metadata={"name": saved.name, "document_type": saved.document_type},
    )

    return success_response(saved.model_dump(mode="json"))


@router.get(
    "/golden-sets/{set_id}",
    summary="Golden Set 조회",
)
def get_golden_set(
    set_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(db_dependency),
):
    repo = GoldenExtractionSetRepository(conn)
    gset = repo.get_by_id(set_id)
    if not gset:
        raise HTTPException(404, "Golden Set을 찾을 수 없습니다.")

    scope = _scope_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
        if gset.scope_profile_id and gset.scope_profile_id != scope:
            raise HTTPException(403, "접근 권한이 없습니다.")

    return success_response(gset.model_dump(mode="json"))
