"""
배치 재추출 API — Phase 8 Task 8-7.

엔드포인트:
  POST /extractions/batch-retry              — 배치 재추출 시작 (202)
  POST /extractions/sample-retry             — 샘플 재추출 시작 (202)
  GET  /extractions/batch-jobs/{job_id}      — 작업 상태 조회
  POST /extractions/batch-jobs/{job_id}/cancel — 취소 요청 (204)
  GET  /extractions/batch-jobs/{job_id}/progress — 진행률 SSE

S2 원칙:
  ⑤ actor_type 감사 로그 (user/agent)
  ⑥ scope_profile_id ACL 슬롯
  ⑦ 폐쇄망 동등성 (LLM fallback 은 ExtractionPipelineService 내부 처리)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.auth.dependencies import resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.responses import success_response
from app.audit.emitter import audit_emitter
from app.db.connection import get_db
from app.models.batch_extraction import (
    BatchJobStatus,
    BatchRetryRequest,
    BatchExtractionJobResponse,
    CancelBatchRequest,
    SampleRetryRequest,
)
from app.repositories.batch_extraction_repository import BatchExtractionJobRepository
from app.services.extraction.batch_extraction_service import run_batch_extraction_background

logger = logging.getLogger(__name__)

router = APIRouter()

_WRITE_ROLES = frozenset({"ORG_ADMIN", "SUPER_ADMIN", "AUTHOR", "EVALUATOR"})


def _actor_id(actor: ActorContext) -> str:
    return str(actor.actor_id) if actor.actor_id else "anonymous"


def _scope_profile_id(actor: ActorContext) -> Optional[UUID]:
    sid = getattr(actor, "scope_profile_id", None)
    if sid and not isinstance(sid, UUID):
        try:
            return UUID(str(sid))
        except Exception:
            return None
    return sid


def _get_llm_provider():
    """LLM provider 인스턴스를 반환한다 (폐쇄망 fallback 포함)."""
    try:
        from app.services.rag_service import get_llm_provider
        return get_llm_provider()
    except Exception:
        from app.services.rag_service import MockLLMProvider
        return MockLLMProvider()


# ---------------------------------------------------------------------------
# POST /extractions/batch-retry
# ---------------------------------------------------------------------------

@router.post(
    "/batch-retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="배치 재추출 시작",
)
def start_batch_retry(
    req: BatchRetryRequest,
    background_tasks: BackgroundTasks,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    """스키마 변경 후 기존 승인 추출을 일괄 재실행한다."""
    if getattr(actor, "role", None) not in _WRITE_ROLES:
        raise HTTPException(403, "배치 재추출 권한이 없습니다.")

    scope_id = _scope_profile_id(actor)
    actor_id = _actor_id(actor)

    repo = BatchExtractionJobRepository(conn)
    job = repo.create(
        extraction_schema_id=req.extraction_schema_id,
        extraction_schema_version=req.extraction_schema_version,
        total_count=0,
        created_by=actor_id,
        scope_profile_id=scope_id,
        date_from=req.date_from,
        date_to=req.date_to,
        sample_count=req.sample_count,
        sample_mode=req.sample_count is not None,
        comparison_mode=req.comparison_mode,
        actor_type=actor.actor_type or "user",
    )
    conn.commit()

    background_tasks.add_task(
        run_batch_extraction_background,
        job_id=str(job.id),
        extraction_schema_id=req.extraction_schema_id,
        extraction_schema_version=req.extraction_schema_version,
        scope_profile_id=str(scope_id) if scope_id else None,
        date_from=req.date_from,
        date_to=req.date_to,
        sample_count=req.sample_count,
        comparison_mode=req.comparison_mode,
        llm_provider=_get_llm_provider(),
    )

    audit_emitter.emit(
        event_type="extraction.batch_retry.started",
        actor_id=actor_id,
        actor_type=actor.actor_type or "user",
        resource_type="batch_extraction_job",
        resource_id=str(job.id),
        metadata={
            "extraction_schema_id": req.extraction_schema_id,
            "sample_count": req.sample_count,
            "comparison_mode": req.comparison_mode,
        },
    )

    return success_response(BatchExtractionJobResponse.from_domain(job))


# ---------------------------------------------------------------------------
# POST /extractions/sample-retry
# ---------------------------------------------------------------------------

@router.post(
    "/sample-retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="샘플 재추출 시작",
)
def start_sample_retry(
    req: SampleRetryRequest,
    background_tasks: BackgroundTasks,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    """새 모델 배포 후 N개 샘플 문서로 성능을 검증한다."""
    if getattr(actor, "role", None) not in _WRITE_ROLES:
        raise HTTPException(403, "샘플 재추출 권한이 없습니다.")

    scope_id = _scope_profile_id(actor)
    actor_id = _actor_id(actor)

    repo = BatchExtractionJobRepository(conn)
    job = repo.create(
        extraction_schema_id=req.extraction_schema_id,
        extraction_schema_version=req.extraction_schema_version,
        total_count=0,
        created_by=actor_id,
        scope_profile_id=scope_id,
        sample_count=req.sample_count,
        sample_mode=True,
        actor_type=actor.actor_type or "user",
    )
    conn.commit()

    background_tasks.add_task(
        run_batch_extraction_background,
        job_id=str(job.id),
        extraction_schema_id=req.extraction_schema_id,
        extraction_schema_version=req.extraction_schema_version,
        scope_profile_id=str(scope_id) if scope_id else None,
        date_from=None,
        date_to=None,
        sample_count=req.sample_count,
        comparison_mode=False,
        llm_provider=_get_llm_provider(),
    )

    audit_emitter.emit(
        event_type="extraction.sample_retry.started",
        actor_id=actor_id,
        actor_type=actor.actor_type or "user",
        resource_type="batch_extraction_job",
        resource_id=str(job.id),
        metadata={
            "extraction_schema_id": req.extraction_schema_id,
            "sample_count": req.sample_count,
        },
    )

    return success_response(BatchExtractionJobResponse.from_domain(job))


# ---------------------------------------------------------------------------
# GET /extractions/batch-jobs/{job_id}
# ---------------------------------------------------------------------------

@router.get(
    "/batch-jobs/{job_id}",
    summary="배치 작업 상태 조회",
)
def get_batch_job(
    job_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    repo = BatchExtractionJobRepository(conn)
    job = repo.get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Batch job not found")

    # scope ACL: 같은 scope 또는 admin
    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and job.scope_profile_id != scope_id:
        raise HTTPException(403, "이 배치 작업에 접근할 권한이 없습니다.")

    return success_response(BatchExtractionJobResponse.from_domain(job))


# ---------------------------------------------------------------------------
# POST /extractions/batch-jobs/{job_id}/cancel
# ---------------------------------------------------------------------------

@router.post(
    "/batch-jobs/{job_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="배치 작업 취소 요청",
)
def cancel_batch_job(
    job_id: UUID,
    req: CancelBatchRequest,
    actor: ActorContext = Depends(resolve_current_actor),
    conn=Depends(get_db),
):
    repo = BatchExtractionJobRepository(conn)
    job = repo.get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Batch job not found")

    scope_id = _scope_profile_id(actor)
    actor_id = _actor_id(actor)
    role = getattr(actor, "role", None)
    if role not in {"ORG_ADMIN", "SUPER_ADMIN"}:
        if job.scope_profile_id != scope_id or job.created_by != actor_id:
            raise HTTPException(403, "이 배치 작업에 접근할 권한이 없습니다.")

    if job.status not in (BatchJobStatus.PENDING, BatchJobStatus.RUNNING):
        raise HTTPException(409, f"취소할 수 없는 상태입니다: {job.status.value}")

    ok = repo.request_cancellation(job_id)
    if not ok:
        raise HTTPException(409, "취소 요청에 실패했습니다.")
    conn.commit()

    audit_emitter.emit(
        event_type="extraction.batch_retry.cancel_requested",
        actor_id=_actor_id(actor),
        actor_type=actor.actor_type or "user",
        resource_type="batch_extraction_job",
        resource_id=str(job_id),
        metadata={"reason": req.reason},
    )


# ---------------------------------------------------------------------------
# GET /extractions/batch-jobs/{job_id}/progress  (SSE)
# ---------------------------------------------------------------------------

@router.get(
    "/batch-jobs/{job_id}/progress",
    summary="배치 작업 진행률 SSE",
)
async def stream_batch_progress(
    job_id: UUID,
    actor: ActorContext = Depends(resolve_current_actor),
):
    """실시간 진행률을 Server-Sent Events로 전송한다."""
    scope_id = _scope_profile_id(actor)
    role = getattr(actor, "role", None)

    return StreamingResponse(
        _progress_generator(job_id, scope_id, role),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _progress_generator(
    job_id: UUID,
    scope_id: Optional[UUID],
    role: Optional[str],
) -> AsyncGenerator[str, None]:
    last_pct = -1.0

    while True:
        with get_db() as conn:
            repo = BatchExtractionJobRepository(conn)
            job = repo.get_by_id(job_id)

        if not job:
            yield f"data: {json.dumps({'error': 'job not found'})}\n\n"
            return

        if role not in {"ORG_ADMIN", "SUPER_ADMIN"} and job.scope_profile_id != scope_id:
            yield f"data: {json.dumps({'error': 'forbidden'})}\n\n"
            return

        if job.progress_percentage != last_pct:
            payload = {
                "status": job.status.value,
                "progress": job.progress_percentage,
                "completed": job.completed_count,
                "failed": job.failed_count,
                "skipped": job.skipped_count,
                "total": job.total_count,
                "estimated_completion_at": (
                    job.estimated_completion_at.isoformat()
                    if job.estimated_completion_at else None
                ),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            last_pct = job.progress_percentage

        if job.status in (
            BatchJobStatus.COMPLETED,
            BatchJobStatus.FAILED,
            BatchJobStatus.CANCELLED,
        ):
            yield f"data: {json.dumps({'done': True, 'status': job.status.value})}\n\n"
            return

        await asyncio.sleep(1)
