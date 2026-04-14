"""Job schedule persistence (Phase 14-14).

책임:
  - job_schedules CRUD (read + schedule/enabled 업데이트만 허용)
  - 최근 실행 이력 조회 (background_jobs 재사용, job_type = schedule.id)
  - 수동 실행 enqueue → background_jobs 에 PENDING 추가 (이후 runner 가 집어감)
  - 실행 중 작업 취소 요청 → background_jobs.status 를 CANCELLED 로 변경 (graceful)

모든 SQL 은 `%s` 파라미터 바인딩. f-string 은 허용 컬럼 화이트리스트에만.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import psycopg2.extensions

logger = logging.getLogger(__name__)


def _row_to_schedule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description"),
        "schedule": row.get("schedule"),
        "enabled": bool(row["enabled"]),
        "last_run_at": row.get("last_run_at"),
        "last_run_duration_ms": row.get("last_run_duration_ms"),
        "last_run_result": row.get("last_run_result"),
        "last_run_id": str(row["last_run_id"]) if row.get("last_run_id") else None,
        "next_run_at": row.get("next_run_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_ms": row.get("duration_ms"),
        "items_processed": row.get("items_processed"),
        "result": row.get("result"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
    }


class JobScheduleRepository:
    # ---- 스케줄 조회 ----
    def list_schedules(
        self, conn: psycopg2.extensions.connection
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, description, schedule, enabled,
                       last_run_at, last_run_duration_ms, last_run_result, last_run_id,
                       next_run_at, created_at, updated_at
                FROM job_schedules
                ORDER BY id
                """
            )
            return [_row_to_schedule(r) for r in cur.fetchall()]

    def get_schedule(
        self, conn: psycopg2.extensions.connection, schedule_id: str
    ) -> Optional[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, description, schedule, enabled,
                       last_run_at, last_run_duration_ms, last_run_result, last_run_id,
                       next_run_at, created_at, updated_at
                FROM job_schedules
                WHERE id = %s
                """,
                (schedule_id,),
            )
            row = cur.fetchone()
            return _row_to_schedule(row) if row else None

    # ---- 스케줄 수정 (화이트리스트 필드) ----
    def update_schedule(
        self,
        conn: psycopg2.extensions.connection,
        schedule_id: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        allowed = {"schedule", "enabled", "next_run_at"}
        set_parts: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            set_parts.append(f"{k} = %s")
            params.append(v)
        if not set_parts:
            return self.get_schedule(conn, schedule_id)
        set_parts.append("updated_at = NOW()")
        params.append(schedule_id)
        sql = f"""
            UPDATE job_schedules SET {', '.join(set_parts)}
            WHERE id = %s
            RETURNING id, name, description, schedule, enabled,
                      last_run_at, last_run_duration_ms, last_run_result, last_run_id,
                      next_run_at, created_at, updated_at
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return _row_to_schedule(row) if row else None

    # ---- 실행 이력 조회 (background_jobs 재사용) ----
    def list_recent_runs(
        self,
        conn: psycopg2.extensions.connection,
        schedule_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, started_at, ended_at,
                       CASE WHEN started_at IS NOT NULL AND ended_at IS NOT NULL
                            THEN CAST(EXTRACT(EPOCH FROM (ended_at - started_at)) * 1000 AS INTEGER)
                            ELSE NULL END AS duration_ms,
                       NULL::int AS items_processed,
                       CASE
                            WHEN status = 'COMPLETED' THEN 'success'
                            WHEN status = 'FAILED' THEN 'failed'
                            WHEN status = 'CANCELLED' THEN 'cancelled'
                            ELSE NULL END AS result,
                       error_code, error_message
                FROM background_jobs
                WHERE job_type = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (schedule_id, int(limit)),
            )
            return [_row_to_run(r) for r in cur.fetchall()]

    def get_running_run(
        self,
        conn: psycopg2.extensions.connection,
        schedule_id: str,
    ) -> Optional[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, started_at, ended_at,
                       NULL::int AS duration_ms,
                       NULL::int AS items_processed,
                       NULL::varchar AS result,
                       error_code, error_message
                FROM background_jobs
                WHERE job_type = %s AND status IN ('PENDING','RUNNING')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (schedule_id,),
            )
            row = cur.fetchone()
            return _row_to_run(row) if row else None

    # ---- 수동 실행 enqueue ----
    def enqueue_manual_run(
        self,
        conn: psycopg2.extensions.connection,
        schedule_id: str,
        *,
        requester_id: Optional[str],
    ) -> str:
        """background_jobs 에 PENDING 레코드 추가. runner 가 픽업한다."""
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO background_jobs (job_type, status, requester_id)
                VALUES (%s, 'PENDING', %s)
                RETURNING id
                """,
                (schedule_id, requester_id),
            )
            return str(cur.fetchone()["id"])

    def mark_cancel_requested(
        self,
        conn: psycopg2.extensions.connection,
        schedule_id: str,
    ) -> Optional[str]:
        """현재 실행 중인 작업을 CANCELLED 상태로 표시 (graceful). 없으면 None."""
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE background_jobs
                SET status = 'CANCELLED', ended_at = NOW()
                WHERE job_type = %s AND status IN ('PENDING','RUNNING')
                RETURNING id
                """,
                (schedule_id,),
            )
            row = cur.fetchone()
            return str(row["id"]) if row else None


job_schedule_repository = JobScheduleRepository()
