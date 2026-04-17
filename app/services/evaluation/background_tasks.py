"""
평가 백그라운드 작업 — Phase 7 FG7.2

FastAPI BackgroundTasks + asyncio 기반 비동기 평가 실행.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from app.observability.logging import log_api_event
from app.repositories.evaluation_repository import (
    EvaluationResultRecordRepository,
    EvaluationRunRepository,
)
from app.services.evaluation.evaluator import Evaluator

logger = logging.getLogger(__name__)


def run_evaluation_background(
    *,
    run_id: str,
    scope_id: str,
    golden_items: List[Dict[str, Any]],
    conn,
) -> None:
    """동기 래퍼 — FastAPI BackgroundTasks에서 호출된다."""
    try:
        asyncio.run(
            _execute_evaluation(
                run_id=run_id,
                scope_id=scope_id,
                golden_items=golden_items,
                conn=conn,
            )
        )
    except Exception as exc:
        logger.error("Background evaluation %s error: %s", run_id, exc)


async def _execute_evaluation(
    *,
    run_id: str,
    scope_id: str,
    golden_items: List[Dict[str, Any]],
    conn,
) -> None:
    run_repo = EvaluationRunRepository(conn)
    result_repo = EvaluationResultRecordRepository(conn)
    evaluator = Evaluator()

    run_repo.update_status(run_id, scope_id, "running")
    try:
        report = await evaluator.evaluate_golden_set_async(
            batch_id=run_id,
            golden_items=golden_items,
            max_concurrent=5,
        )
        result_repo.batch_insert(run_id, report.results)
        run_repo.finalize(
            run_id=run_id,
            scope_id=scope_id,
            successful_items=report.successful_items,
            failed_items=report.failed_items,
            overall_score=report.overall_score(),
            total_tokens=report.total_tokens,
            total_latency_ms=report.total_latency_ms,
            total_cost=report.total_cost,
            duration_seconds=report.duration_seconds or 0.0,
        )
        log_api_event(
            event_type="evaluation.completed",
            actor_type="system",
            resource_type="evaluation_run",
            resource_id=run_id,
            result="success",
            extra={"successful_items": report.successful_items},
        )
        logger.info("Evaluation %s completed: %d items", run_id, report.successful_items)
    except Exception as exc:
        log_api_event(
            event_type="evaluation.failed",
            actor_type="system",
            resource_type="evaluation_run",
            resource_id=run_id,
            result="failure",
            extra={"error": str(exc)},
        )
        logger.error("Evaluation %s failed: %s", run_id, exc, exc_info=True)
        run_repo.update_status(run_id, scope_id, "failed")
        raise
