"""
BatchExtractionJobRepository — Phase 8 Task 8-7.

raw psycopg2 기반 CRUD.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID, uuid4

import psycopg2.extras

from app.models.batch_extraction import BatchExtractionJob, BatchJobStatus, ExtractionRetryLog

logger = logging.getLogger(__name__)


def _row_to_job(row: dict) -> BatchExtractionJob:
    return BatchExtractionJob(
        id=UUID(str(row["id"])),
        extraction_schema_id=row["extraction_schema_id"],
        extraction_schema_version=row["extraction_schema_version"],
        scope_profile_id=UUID(str(row["scope_profile_id"])) if row.get("scope_profile_id") else None,
        status=BatchJobStatus(row["status"]),
        total_count=row["total_count"],
        completed_count=row["completed_count"],
        failed_count=row["failed_count"],
        skipped_count=row["skipped_count"],
        progress_percentage=float(row["progress_percentage"]),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        sample_count=row.get("sample_count"),
        sample_mode=bool(row["sample_mode"]),
        comparison_mode=bool(row["comparison_mode"]),
        comparison_report_path=row.get("comparison_report_path"),
        created_at=row["created_at"],
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        estimated_completion_at=row.get("estimated_completion_at"),
        current_processing=row.get("current_processing"),
        error_summary=row.get("error_summary"),
        failed_document_ids=row.get("failed_document_ids") or [],
        created_by=str(row["created_by"]),
        is_cancellation_requested=bool(row["is_cancellation_requested"]),
        actor_type=row.get("actor_type", "user"),
    )


class BatchExtractionJobRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    def create(
        self,
        *,
        extraction_schema_id: str,
        extraction_schema_version: int,
        total_count: int,
        created_by: str,
        scope_profile_id: Optional[UUID] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        sample_count: Optional[int] = None,
        sample_mode: bool = False,
        comparison_mode: bool = False,
        actor_type: str = "user",
    ) -> BatchExtractionJob:
        sql = """
            INSERT INTO batch_extraction_jobs (
                extraction_schema_id, extraction_schema_version,
                scope_profile_id, status, total_count,
                date_from, date_to, sample_count, sample_mode,
                comparison_mode, created_by, actor_type
            ) VALUES (
                %(schema_id)s, %(schema_ver)s,
                %(scope)s, 'pending', %(total)s,
                %(date_from)s, %(date_to)s, %(sample_count)s, %(sample_mode)s,
                %(comparison_mode)s, %(created_by)s, %(actor_type)s
            )
            RETURNING *
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {
                "schema_id": extraction_schema_id,
                "schema_ver": extraction_schema_version,
                "scope": str(scope_profile_id) if scope_profile_id else None,
                "total": total_count,
                "date_from": date_from,
                "date_to": date_to,
                "sample_count": sample_count,
                "sample_mode": sample_mode,
                "comparison_mode": comparison_mode,
                "created_by": created_by,
                "actor_type": actor_type,
            })
            row = cur.fetchone()
        return _row_to_job(dict(row))

    def get_by_id(self, job_id: UUID) -> Optional[BatchExtractionJob]:
        sql = "SELECT * FROM batch_extraction_jobs WHERE id = %s"
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(job_id),))
            row = cur.fetchone()
        return _row_to_job(dict(row)) if row else None

    def list_by_scope(
        self,
        scope_profile_id: Optional[UUID],
        status: Optional[BatchJobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[BatchExtractionJob]:
        conditions = ["1=1"]
        params: list = []
        if scope_profile_id:
            conditions.append("scope_profile_id = %s")
            params.append(str(scope_profile_id))
        if status:
            conditions.append("status = %s")
            params.append(status.value)
        params += [limit, offset]
        sql = f"""
            SELECT * FROM batch_extraction_jobs
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_row_to_job(dict(r)) for r in rows]

    def update_status(
        self,
        job_id: UUID,
        new_status: BatchJobStatus,
        error_summary: Optional[str] = None,
    ) -> Optional[BatchExtractionJob]:
        now = datetime.now(timezone.utc)
        extra_sets = []
        params: dict = {
            "status": new_status.value,
            "job_id": str(job_id),
        }
        if new_status == BatchJobStatus.RUNNING:
            extra_sets.append("started_at = COALESCE(started_at, %(started_at)s)")
            params["started_at"] = now
        if new_status in (BatchJobStatus.COMPLETED, BatchJobStatus.FAILED, BatchJobStatus.CANCELLED):
            extra_sets.append("completed_at = %(completed_at)s")
            params["completed_at"] = now
        if error_summary is not None:
            extra_sets.append("error_summary = %(error_summary)s")
            params["error_summary"] = error_summary

        set_clause = "status = %(status)s"
        if extra_sets:
            set_clause += ", " + ", ".join(extra_sets)

        sql = f"""
            UPDATE batch_extraction_jobs
            SET {set_clause}
            WHERE id = %(job_id)s
            RETURNING *
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_job(dict(row)) if row else None

    def update_progress(
        self,
        job_id: UUID,
        completed: int,
        failed: int,
        skipped: int,
        total: int,
    ) -> Optional[BatchExtractionJob]:
        processed = completed + failed + skipped
        progress = round((processed / total) * 100.0, 2) if total > 0 else 0.0

        # 예상 완료 시간 계산: 현재 시간 기준 elapsed 없이 진행률로 단순 추정
        sql = """
            UPDATE batch_extraction_jobs
            SET completed_count = %(completed)s,
                failed_count     = %(failed)s,
                skipped_count    = %(skipped)s,
                progress_percentage = %(progress)s,
                current_processing  = %(current)s
            WHERE id = %(job_id)s
            RETURNING *
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "progress": progress,
                "current": processed,
                "job_id": str(job_id),
            })
            row = cur.fetchone()
        return _row_to_job(dict(row)) if row else None

    def append_failed_document(self, job_id: UUID, document_id: UUID) -> None:
        sql = """
            UPDATE batch_extraction_jobs
            SET failed_document_ids = failed_document_ids || %(doc_id)s::jsonb
            WHERE id = %(job_id)s
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, {
                "doc_id": json.dumps([str(document_id)]),
                "job_id": str(job_id),
            })

    def request_cancellation(self, job_id: UUID) -> bool:
        sql = """
            UPDATE batch_extraction_jobs
            SET is_cancellation_requested = TRUE
            WHERE id = %s
              AND status IN ('pending', 'running')
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (str(job_id),))
            return cur.fetchone() is not None

    def is_cancellation_requested(self, job_id: UUID) -> bool:
        sql = "SELECT is_cancellation_requested FROM batch_extraction_jobs WHERE id = %s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (str(job_id),))
            row = cur.fetchone()
        return bool(row[0]) if row else False


class ExtractionRetryLogRepository:
    def __init__(self, conn) -> None:
        self._conn = conn

    def create(
        self,
        *,
        job_id: UUID,
        document_id: UUID,
        attempt_number: int,
        status: str,
        error_reason: Optional[str],
        latency_ms: int,
    ) -> None:
        sql = """
            INSERT INTO extraction_retry_logs
                (job_id, document_id, attempt_number, status, error_reason, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                str(job_id),
                str(document_id),
                attempt_number,
                status,
                error_reason,
                latency_ms,
            ))

    def list_by_job(self, job_id: UUID) -> list:
        sql = """
            SELECT * FROM extraction_retry_logs
            WHERE job_id = %s
            ORDER BY created_at DESC
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(job_id),))
            return [dict(r) for r in cur.fetchall()]
