"""
평가 결과 Repository (Data Access Layer) — Phase 7 FG7.2

psycopg2 raw SQL 기반.
S2 원칙 ⑥: scope_id 기반 ACL 필터 필수 적용.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.services.evaluation.models import EvaluationResult
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


class EvaluationRunRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    def create(
        self,
        *,
        batch_id: str,
        scope_id: str,
        actor_id: str,
        actor_type: str,
        total_items: int,
        golden_set_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        run_id = str(uuid4())
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evaluation_runs
                    (id, batch_id, scope_id, actor_id, actor_type,
                     total_items, golden_set_id, metadata_json, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued')
                RETURNING id, batch_id, status, scope_id, actor_id,
                          actor_type, total_items, created_at
                """,
                (
                    run_id, batch_id, scope_id, actor_id, actor_type,
                    total_items, golden_set_id,
                    json.dumps(metadata or {}),
                ),
            )
            row = cur.fetchone()
        logger.info("Created evaluation_run %s", run_id)
        return self._row_to_dict(row)

    def get_by_id(self, run_id: str, scope_id: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, batch_id, status, scope_id, actor_id, actor_type,
                       total_items, successful_items, failed_items,
                       overall_score, total_tokens, total_latency_ms, total_cost,
                       duration_seconds, started_at, completed_at, created_at
                FROM evaluation_runs
                WHERE id = %s AND scope_id = %s
                """,
                (run_id, scope_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def list_by_scope(
        self,
        scope_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
    ) -> tuple[List[Dict], int]:
        where = "WHERE scope_id = %s"
        params: list = [scope_id]
        if status:
            where += " AND status = %s"
            params.append(status)

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM evaluation_runs {where}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"""
                SELECT id, batch_id, status, scope_id, actor_id, actor_type,
                       total_items, successful_items, failed_items, overall_score,
                       duration_seconds, created_at, completed_at
                FROM evaluation_runs {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            rows = cur.fetchall()
        # psycopg2 RealDictCursor → 각 row 는 이미 dict-like (RealDictRow).
        return [dict(r) for r in rows], total

    def update_status(self, run_id: str, scope_id: str, new_status: str) -> bool:
        now = utcnow()
        extra_fields = ""
        extra_vals: list = []
        if new_status == "running":
            extra_fields = ", started_at = %s"
            extra_vals = [now]
        elif new_status in ("completed", "failed"):
            extra_fields = ", completed_at = %s"
            extra_vals = [now]

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE evaluation_runs
                SET status = %s, updated_at = NOW(){extra_fields}
                WHERE id = %s AND scope_id = %s
                """,
                [new_status, *extra_vals, run_id, scope_id],
            )
            return cur.rowcount > 0

    def finalize(
        self,
        run_id: str,
        scope_id: str,
        *,
        successful_items: int,
        failed_items: int,
        overall_score: float,
        total_tokens: int,
        total_latency_ms: float,
        total_cost: float,
        duration_seconds: float,
    ) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evaluation_runs
                SET status = 'completed',
                    successful_items = %s,
                    failed_items = %s,
                    overall_score = %s,
                    total_tokens = %s,
                    total_latency_ms = %s,
                    total_cost = %s,
                    duration_seconds = %s,
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s AND scope_id = %s
                """,
                (
                    successful_items, failed_items, overall_score,
                    total_tokens, total_latency_ms, total_cost,
                    duration_seconds, run_id, scope_id,
                ),
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        # psycopg2 RealDictCursor → row 는 이미 dict-like (RealDictRow).
        return dict(row) if row is not None else {}


class EvaluationResultRecordRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    def batch_insert(self, run_id: str, results: List[EvaluationResult]) -> int:
        inserted = 0
        with self._conn.cursor() as cur:
            for r in results:
                cur.execute(
                    """
                    INSERT INTO evaluation_result_records
                        (id, run_id, item_id, question, answer, contexts,
                         expected_answer, expected_sources,
                         faithfulness, answer_relevance, context_precision,
                         context_recall, citation_present_rate, hallucination_rate,
                         overall_score, retrieval_ms, generation_ms,
                         total_latency_ms, input_tokens, output_tokens,
                         total_tokens, estimated_cost, evaluator_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid4()), run_id, r.item_id,
                        r.question, r.answer,
                        json.dumps(r.contexts),
                        r.expected_answer,
                        json.dumps(r.expected_sources) if r.expected_sources else None,
                        r.scores.faithfulness,
                        r.scores.answer_relevance,
                        r.scores.context_precision,
                        r.scores.context_recall,
                        r.scores.citation_present_rate,
                        r.scores.hallucination_rate,
                        r.overall_score(),
                        r.latency_metrics.retrieval_ms if r.latency_metrics else None,
                        r.latency_metrics.generation_ms if r.latency_metrics else None,
                        r.latency_metrics.total_ms if r.latency_metrics else None,
                        r.token_metrics.query_tokens if r.token_metrics else None,
                        r.token_metrics.response_tokens if r.token_metrics else None,
                        r.token_metrics.total_tokens if r.token_metrics else None,
                        r.cost_metrics.total_cost if r.cost_metrics else None,
                        r.evaluator_version,
                    ),
                )
                inserted += 1
        logger.info("Inserted %d result records for run %s", inserted, run_id)
        return inserted

    def list_by_run(self, run_id: str, *, offset: int = 0, limit: int = 1000) -> List[Dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, item_id, question, answer,
                       faithfulness, answer_relevance, context_precision,
                       context_recall, citation_present_rate, hallucination_rate,
                       overall_score, total_latency_ms, total_tokens, estimated_cost,
                       created_at
                FROM evaluation_result_records
                WHERE run_id = %s
                ORDER BY created_at
                LIMIT %s OFFSET %s
                """,
                (run_id, limit, offset),
            )
            rows = cur.fetchall()
        # psycopg2 RealDictCursor → 각 row 는 이미 dict-like (RealDictRow).
        return [dict(r) for r in rows]
