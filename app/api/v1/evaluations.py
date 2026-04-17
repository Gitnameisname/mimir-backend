"""
평가 API 라우터 — Phase 7 FG7.2

엔드포인트:
  POST   /evaluations/run                 — 평가 작업 시작
  GET    /evaluations                     — 평가 목록 조회
  GET    /evaluations/{eval_id}           — 평가 결과 상세 조회
  GET    /evaluations/{eval_id}/compare   — 두 평가 비교

S2 원칙 ⑤: actor_type 감사 로그 기록
S2 원칙 ⑥: scope_id 기반 ACL 필터 필수
S2 원칙 ⑦: 폐쇄망 환경 지원
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import list_response, success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.repositories.evaluation_repository import (
    EvaluationResultRecordRepository,
    EvaluationRunRepository,
)
from app.services.evaluation.background_tasks import run_evaluation_background

logger = logging.getLogger(__name__)

router = APIRouter(tags=["evaluations"])

_WRITE_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN", "AUTHOR", "EVALUATOR"})


def _require_scope(actor: ActorContext) -> str:
    sid = getattr(actor, "scope_profile_id", None)
    if not sid:
        raise HTTPException(403, "Scope Profile이 바인딩되지 않은 계정입니다.")
    return str(sid)


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------

class EvaluationRunRequest(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=200)
    golden_set_id: Optional[str] = None
    golden_items: List[Dict[str, Any]] = Field(..., min_length=1)
    max_concurrent: int = Field(default=5, ge=1, le=20)

    @field_validator("golden_items")
    @classmethod
    def _validate_golden_items(cls, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"golden_items[{i}]는 dict여야 합니다")
            if "question" not in item:
                raise ValueError(f"golden_items[{i}]에 'question' 필드가 필요합니다")
            if "expected_answer" not in item:
                raise ValueError(f"golden_items[{i}]에 'expected_answer' 필드가 필요합니다")
        return items


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------

@router.post("/run")
def run_evaluation(
    request: EvaluationRunRequest,
    background_tasks: BackgroundTasks,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    scope_id = _require_scope(actor)
    if getattr(actor, "role", None) not in _WRITE_ROLES:
        raise HTTPException(403, "평가 실행 권한이 없습니다.")

    run_repo = EvaluationRunRepository(conn)
    run = run_repo.create(
        batch_id=request.batch_id,
        scope_id=scope_id,
        actor_id=str(actor.actor_id),
        actor_type=actor.actor_type,
        total_items=len(request.golden_items),
        golden_set_id=request.golden_set_id,
    )

    background_tasks.add_task(
        run_evaluation_background,
        run_id=run["id"],
        scope_id=scope_id,
        golden_items=request.golden_items,
        conn=conn,
    )

    audit_emitter.emit(
        event_type="evaluation.run.started",
        actor_id=str(actor.actor_id),
        actor_type=actor.actor_type,
        resource_type="evaluation_run",
        resource_id=run["id"],
        metadata={
            "batch_id": request.batch_id,
            "total_items": len(request.golden_items),
        },
    )
    logger.info("Evaluation run %s queued by %s (%s)", run["id"], actor.actor_id, actor.actor_type)

    return success_response({
        "evaluation_id": run["id"],
        "batch_id": run["batch_id"],
        "status": run["status"],
        "total_items": run["total_items"],
        "created_at": run["created_at"].isoformat() if run.get("created_at") else None,
        "message": "평가 작업이 큐에 등록되었습니다.",
    })


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

@router.get("")
def list_evaluations(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    scope_id = _require_scope(actor)
    run_repo = EvaluationRunRepository(conn)
    items, total = run_repo.list_by_scope(scope_id, offset=offset, limit=limit, status=status)

    audit_emitter.emit(
        event_type="evaluation.run.list",
        actor_id=str(actor.actor_id),
        actor_type=actor.actor_type,
        resource_type="evaluation_run",
        resource_id=None,
        metadata={"total": total},
    )
    return list_response(items, total=total, offset=offset, limit=limit)


# ---------------------------------------------------------------------------
# GET /{eval_id}
# ---------------------------------------------------------------------------

@router.get("/{eval_id}")
def get_evaluation(
    eval_id: str,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    scope_id = _require_scope(actor)
    run_repo = EvaluationRunRepository(conn)
    result_repo = EvaluationResultRecordRepository(conn)

    run = run_repo.get_by_id(eval_id, scope_id)
    if not run:
        raise HTTPException(404, "평가 결과를 찾을 수 없습니다.")

    results = result_repo.list_by_run(eval_id)

    audit_emitter.emit(
        event_type="evaluation.run.read",
        actor_id=str(actor.actor_id),
        actor_type=actor.actor_type,
        resource_type="evaluation_run",
        resource_id=eval_id,
        metadata={},
    )
    return success_response({**run, "results": results})


# ---------------------------------------------------------------------------
# GET /{eval_id}/compare
# ---------------------------------------------------------------------------

@router.get("/{eval_id}/compare")
def compare_evaluations(
    eval_id: str,
    eval_id2: str = Query(..., description="두 번째 평가 ID"),
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    scope_id = _require_scope(actor)
    run_repo = EvaluationRunRepository(conn)
    result_repo = EvaluationResultRecordRepository(conn)

    run1 = run_repo.get_by_id(eval_id, scope_id)
    run2 = run_repo.get_by_id(eval_id2, scope_id)

    if not run1 or not run2:
        raise HTTPException(404, "하나 이상의 평가를 찾을 수 없습니다.")

    results1 = result_repo.list_by_run(eval_id)
    results2 = result_repo.list_by_run(eval_id2)

    _METRICS = [
        "faithfulness", "answer_relevance", "context_precision",
        "context_recall", "citation_present_rate", "hallucination_rate",
    ]

    def _avg(records: List[Dict], field: str) -> Optional[float]:
        vals = [r[field] for r in records if r.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    comparison: Dict[str, Any] = {
        "eval_id_1": eval_id,
        "eval_id_2": eval_id2,
        "metric_comparison": {
            m: {
                "eval1": _avg(results1, m),
                "eval2": _avg(results2, m),
                "difference": (
                    (_avg(results2, m) or 0) - (_avg(results1, m) or 0)
                ),
            }
            for m in _METRICS
        },
        "overall_score_1": run1.get("overall_score"),
        "overall_score_2": run2.get("overall_score"),
        "improvement": (run2.get("overall_score") or 0) - (run1.get("overall_score") or 0),
    }

    audit_emitter.emit(
        event_type="evaluation.run.compare",
        actor_id=str(actor.actor_id),
        actor_type=actor.actor_type,
        resource_type="evaluation_run",
        resource_id=eval_id,
        metadata={"eval_id2": eval_id2},
    )
    return success_response(comparison)
