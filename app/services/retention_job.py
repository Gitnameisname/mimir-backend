"""Retention 배치 작업 — S3 Phase 6 FG 6-2 (2026-05-18).

운영 안전 R-O2 (Phase 6 §1.2): retention 은 **archive-first** — 데이터 삭제 전
별 archive 테이블에 INSERT INTO ... SELECT + DELETE 트랜잭션으로 이동한다.
직접 DELETE 금지.

대상:
  1. ``audit_events`` event_type='document.viewed' 가 ``RETENTION_DOCUMENT_VIEWED_DAYS``
     일 (기본 7일) 경과 → ``audit_events_archive`` 로 이동.
  2. ``annotations`` status='resolved' 가 ``RETENTION_RESOLVED_ANNOTATION_DAYS``
     일 (기본 90일) 경과 → cascade 답글 포함 ``annotations_archive`` 로 이동.

환경변수:
  - ``RETENTION_DOCUMENT_VIEWED_DAYS``     (int, 기본 7)
  - ``RETENTION_RESOLVED_ANNOTATION_DAYS`` (int, 기본 90)
  - ``RETENTION_BATCH_LIMIT``              (int, 기본 500) — 한 번에 처리할 row 상한.
  - ``RETENTION_DRY_RUN``                  ("1"/"true" 시 dry-run, archive/DELETE 안 함)
  - ``RETENTION_CRON_HOUR``                (int, 기본 2) — scheduler 가 사용.

호출자:
  - ``app.scheduler.BatchScheduler`` 가 cron 으로 호출 (off-peak 시간 1회).
  - 운영자가 admin job runner 를 통해 수동 실행 (예: 첫 실행 시 dry-run).

S2 원칙 ⑦ (폐쇄망 호환): 외부 의존 없음 — psycopg2 + 표준 SQL.

설계 원칙:
  - 단일 트랜잭션 안에서 INSERT + DELETE — 실패 시 rollback 으로 원본 보존.
  - dry-run 모드 는 archive 후보 카운트만 반환, DB 변경 없음.
  - 배치 실패가 다른 batch 의 진행을 막지 않도록 try/except.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import psycopg2.extensions

from app.utils.time import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "RetentionJob",
    "run_retention_job",
    "DEFAULT_VIEWED_DAYS",
    "DEFAULT_RESOLVED_ANNOTATION_DAYS",
    "DEFAULT_BATCH_LIMIT",
]

DEFAULT_VIEWED_DAYS: int = 7
DEFAULT_RESOLVED_ANNOTATION_DAYS: int = 90
DEFAULT_BATCH_LIMIT: int = 500


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("retention_job: invalid env %s=%r, using default %d", key, raw, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


class RetentionJob:
    """archive-first retention batch.

    psycopg2 connection 을 받아 두 종류의 retention 을 순서대로 실행한다.
    호출자가 connection 라이프사이클 관리.
    """

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        *,
        viewed_days: Optional[int] = None,
        resolved_annotation_days: Optional[int] = None,
        batch_limit: Optional[int] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self._conn = conn
        self._viewed_days = (
            viewed_days
            if viewed_days is not None
            else _env_int("RETENTION_DOCUMENT_VIEWED_DAYS", DEFAULT_VIEWED_DAYS)
        )
        self._resolved_days = (
            resolved_annotation_days
            if resolved_annotation_days is not None
            else _env_int(
                "RETENTION_RESOLVED_ANNOTATION_DAYS", DEFAULT_RESOLVED_ANNOTATION_DAYS,
            )
        )
        self._batch_limit = (
            batch_limit
            if batch_limit is not None
            else _env_int("RETENTION_BATCH_LIMIT", DEFAULT_BATCH_LIMIT)
        )
        self._dry_run = dry_run if dry_run is not None else _env_bool("RETENTION_DRY_RUN")

    # ------------------------------------------------------------------
    # 실행 엔트리
    # ------------------------------------------------------------------

    def run(self, *, request_id: Optional[str] = None) -> dict[str, Any]:
        started_at = utcnow()
        logger.info(
            "retention_job_start dry_run=%s viewed_days=%d resolved_days=%d batch_limit=%d request_id=%s",
            self._dry_run, self._viewed_days, self._resolved_days, self._batch_limit, request_id,
        )

        result: dict[str, Any] = {
            "status": "success",
            "dry_run": self._dry_run,
            "document_viewed": {"candidates": 0, "archived": 0, "deleted": 0},
            "resolved_annotations": {"candidates": 0, "archived": 0, "deleted": 0},
            "errors": [],
        }

        try:
            audit_result = self._run_audit_view_retention()
            result["document_viewed"] = audit_result
        except Exception as exc:
            logger.error("retention_job audit step failed: %s", exc)
            result["errors"].append(f"audit_events: {exc}")
            self._safe_rollback()

        try:
            ann_result = self._run_annotations_retention()
            result["resolved_annotations"] = ann_result
        except Exception as exc:
            logger.error("retention_job annotations step failed: %s", exc)
            result["errors"].append(f"annotations: {exc}")
            self._safe_rollback()

        if result["errors"]:
            result["status"] = "partial"

        elapsed = (utcnow() - started_at).total_seconds()
        logger.info(
            "retention_job_complete dry_run=%s elapsed_s=%.2f viewed=%s annotations=%s errors=%d",
            self._dry_run, elapsed,
            result["document_viewed"], result["resolved_annotations"],
            len(result["errors"]),
        )
        return result

    # ------------------------------------------------------------------
    # audit_events.document.viewed → audit_events_archive
    # ------------------------------------------------------------------

    def _run_audit_view_retention(self) -> dict[str, int]:
        cutoff_sql = "NOW() - (%s || ' days')::interval"
        params = (str(self._viewed_days),)
        # 후보 카운트 (dry-run 또는 metrics).
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS c
                FROM audit_events
                WHERE event_type = 'document.viewed'
                  AND occurred_at < {cutoff_sql}
                """,
                params,
            )
            row = cur.fetchone()
            candidates = int((row.get("c") if isinstance(row, dict) else row[0]) or 0) if row else 0

        if self._dry_run or candidates == 0:
            return {"candidates": candidates, "archived": 0, "deleted": 0}

        # archive-first — 단일 트랜잭션 안에 INSERT + DELETE.
        # Codex 2차 P1-1 (2026-05-18) 시정:
        #   - DELETE 기준을 "archive 성공 id 집합" 으로 제한 (이전엔 expired 후보).
        #   - 이전 retry 등으로 archive 에 이미 존재하던 row 도 같이 source 에서 정리
        #     해야 batch idempotency 가 보장된다 → already_archived CTE 로 union.
        #   - archive 성공 / 이미 존재 외 어떤 row 도 DELETE 대상이 되지 않는다.
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                WITH expired AS (
                    SELECT id
                    FROM audit_events
                    WHERE event_type = 'document.viewed'
                      AND occurred_at < {cutoff_sql}
                    ORDER BY occurred_at ASC
                    LIMIT %s
                ),
                inserted AS (
                    INSERT INTO audit_events_archive (
                        id, event_type, occurred_at, actor_user_id, actor_role,
                        document_id, version_id, target_version_id,
                        previous_state, new_state, action_result, reason, request_id
                    )
                    SELECT
                        ae.id, ae.event_type, ae.occurred_at, ae.actor_user_id, ae.actor_role,
                        ae.document_id, ae.version_id, ae.target_version_id,
                        ae.previous_state, ae.new_state, ae.action_result, ae.reason, ae.request_id
                    FROM audit_events ae
                    INNER JOIN expired e ON e.id = ae.id
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                ),
                already_archived AS (
                    SELECT a.id
                    FROM audit_events_archive a
                    INNER JOIN expired e ON e.id = a.id
                ),
                deletable AS (
                    SELECT id FROM inserted
                    UNION
                    SELECT id FROM already_archived
                )
                DELETE FROM audit_events
                WHERE id IN (SELECT id FROM deletable)
                RETURNING id
                """,
                (str(self._viewed_days), self._batch_limit),
            )
            deleted_rows = cur.fetchall()
            deleted = len(deleted_rows)
        self._conn.commit()
        # archived 는 "archive 테이블에 존재 보장된 id 수" — 이번 회차 INSERT + 이전
        # 회차 잔존 모두 포함. deleted 와 동치 (deletable 의 정의).
        return {"candidates": candidates, "archived": deleted, "deleted": deleted}

    # ------------------------------------------------------------------
    # annotations.status='resolved' (+ cascade replies) → annotations_archive
    # ------------------------------------------------------------------

    def _run_annotations_retention(self) -> dict[str, int]:
        cutoff_sql = "NOW() - (%s || ' days')::interval"
        # resolved root (parent_id IS NULL) 만 후보 — 답글은 cascade 로 함께 이동.
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS c
                FROM annotations
                WHERE status = 'resolved'
                  AND parent_id IS NULL
                  AND resolved_at IS NOT NULL
                  AND resolved_at < {cutoff_sql}
                """,
                (str(self._resolved_days),),
            )
            row = cur.fetchone()
            candidates = int((row.get("c") if isinstance(row, dict) else row[0]) or 0) if row else 0

        if self._dry_run or candidates == 0:
            return {"candidates": candidates, "archived": 0, "deleted": 0}

        # Codex 2차 P1-1 (2026-05-18) 시정:
        #   (a) **recursive CTE** 로 root 의 전체 descendant (nested replies 임의 depth)
        #       를 수집 → annotations.parent_id FK ON DELETE CASCADE 가 archive
        #       전에 답글을 삭제하는 경로 차단.
        #   (b) DELETE 기준을 archive 성공 + already_archived UNION 으로 제한 →
        #       어떤 상황에서도 archive 미통과 row 가 source 에서 사라지지 않음.
        #   (c) archived row 수와 deleted row 수 가 일치하지 않으면 rollback +
        #       error 반환 → archive-first 위반을 즉시 가시화.
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                WITH RECURSIVE expired_roots AS (
                    SELECT id
                    FROM annotations
                    WHERE status = 'resolved'
                      AND parent_id IS NULL
                      AND resolved_at IS NOT NULL
                      AND resolved_at < {cutoff_sql}
                    ORDER BY resolved_at ASC
                    LIMIT %s
                ),
                descendants AS (
                    SELECT a.id, a.parent_id
                    FROM annotations a
                    INNER JOIN expired_roots r ON a.id = r.id
                    UNION ALL
                    SELECT a.id, a.parent_id
                    FROM annotations a
                    INNER JOIN descendants d ON a.parent_id = d.id
                ),
                expired_all AS (
                    SELECT DISTINCT id FROM descendants
                ),
                inserted AS (
                    INSERT INTO annotations_archive (
                        id, document_id, version_id, node_id, span_start, span_end,
                        author_id, actor_type, content, status, resolved_at, resolved_by,
                        parent_id, is_orphan, orphaned_at, created_at, updated_at
                    )
                    SELECT a.id, a.document_id, a.version_id, a.node_id, a.span_start, a.span_end,
                           a.author_id, a.actor_type, a.content, a.status, a.resolved_at, a.resolved_by,
                           a.parent_id, a.is_orphan, a.orphaned_at, a.created_at, a.updated_at
                    FROM annotations a
                    INNER JOIN expired_all e ON e.id = a.id
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                ),
                already_archived AS (
                    SELECT a.id
                    FROM annotations_archive a
                    INNER JOIN expired_all e ON e.id = a.id
                ),
                deletable AS (
                    SELECT id FROM inserted
                    UNION
                    SELECT id FROM already_archived
                )
                DELETE FROM annotations
                WHERE id IN (SELECT id FROM deletable)
                RETURNING id
                """,
                (str(self._resolved_days), self._batch_limit),
            )
            deleted_rows = cur.fetchall()
            deleted = len(deleted_rows)

            # archive-first 무결성 게이트: DELETE 한 모든 id 가 archive 에 존재해야 함.
            #   ON DELETE CASCADE 가 archive 안 거친 reply 를 함께 삭제하면 여기서 발견.
            if deleted > 0:
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS c
                    FROM (
                        SELECT id FROM annotations_archive
                        WHERE id = ANY(%s)
                    ) t
                    """,
                    ([self._row_id(r) for r in deleted_rows],),
                )
                row = cur.fetchone()
                archived_count = int(
                    (row.get("c") if isinstance(row, dict) else row[0]) or 0
                ) if row else 0
                if archived_count != deleted:
                    # 데이터 손실 가능성 — rollback 후 error.
                    self._conn.rollback()
                    raise RuntimeError(
                        "annotation retention archive-first violation: "
                        f"deleted={deleted} archived={archived_count}"
                    )

        self._conn.commit()
        return {"candidates": candidates, "archived": deleted, "deleted": deleted}

    @staticmethod
    def _row_id(row) -> str:
        """RETURNING id 결과 row 에서 id 값 추출 (dict / tuple 모두 지원)."""
        if isinstance(row, dict):
            return row.get("id")
        return row[0]

    # ------------------------------------------------------------------
    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception as exc:
            logger.warning("retention_job rollback 실패: %s", exc)


def run_retention_job(*, request_id: Optional[str] = None) -> dict[str, Any]:
    """scheduler 가 호출하는 모듈 진입점. 새 connection 으로 실행."""
    try:
        from app.db import get_db
        with get_db() as conn:
            job = RetentionJob(conn)
            return job.run(request_id=request_id)
    except Exception as exc:
        logger.error("retention_job entrypoint error: %s", exc)
        return {"status": "error", "dry_run": False, "errors": [str(exc)]}
