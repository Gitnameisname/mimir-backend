"""
WorkflowRepository — Phase 5 Workflow 관련 테이블 접근.

담당 테이블:
  - review_actions    : 검토/승인/반려 액션 기록
  - workflow_history  : 상태 전이 이력 (immutable)
  - change_logs       : 변경 사유 기록

versions 테이블의 workflow_status 컬럼 업데이트도 이 repository가 담당한다.
"""

import logging
from datetime import datetime
from typing import Any, Optional

import psycopg2.extensions

from app.models.change_log import ChangeLog
from app.models.review_action import ReviewAction
from app.models.workflow_history import WorkflowHistory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row 변환 헬퍼
# ---------------------------------------------------------------------------


def _row_to_review_action(row: dict[str, Any]) -> ReviewAction:
    return ReviewAction(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        version_id=str(row["version_id"]),
        action_type=row["action_type"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        actor_id=row.get("actor_id"),
        actor_role=row.get("actor_role"),
        comment=row.get("comment"),
        reason=row.get("reason"),
        created_at=row["created_at"],
        metadata=row["metadata"] if row.get("metadata") is not None else {},
    )


def _row_to_workflow_history(row: dict[str, Any]) -> WorkflowHistory:
    return WorkflowHistory(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        version_id=str(row["version_id"]),
        from_status=row["from_status"],
        to_status=row["to_status"],
        action=row["action"],
        actor_id=row.get("actor_id"),
        actor_role=row.get("actor_role"),
        comment=row.get("comment"),
        reason=row.get("reason"),
        created_at=row["created_at"],
    )


def _row_to_change_log(row: dict[str, Any]) -> ChangeLog:
    return ChangeLog(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        version_id=str(row["version_id"]) if row.get("version_id") else None,
        change_type=row["change_type"],
        reason=row.get("reason"),
        actor_id=row.get("actor_id"),
        actor_role=row.get("actor_role"),
        metadata=row["metadata"] if row.get("metadata") is not None else {},
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# WorkflowRepository
# ---------------------------------------------------------------------------


class WorkflowRepository:

    # -----------------------------------------------------------------------
    # versions.workflow_status 갱신
    # -----------------------------------------------------------------------

    def update_workflow_status(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        new_status: str,
    ) -> None:
        """versions 테이블의 workflow_status 컬럼을 갱신한다."""
        sql = """
            UPDATE versions
            SET workflow_status = %s
            WHERE id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (new_status, version_id))
        logger.debug("workflow_status updated: version=%s → %s", version_id, new_status)

    def get_workflow_status(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> Optional[str]:
        """특정 버전의 현재 workflow_status를 반환한다."""
        sql = "SELECT workflow_status FROM versions WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return row["workflow_status"]

    # -----------------------------------------------------------------------
    # review_actions
    # -----------------------------------------------------------------------

    def create_review_action(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        action_type: str,
        from_status: str,
        to_status: str,
        actor_id: Optional[str] = None,
        actor_role: Optional[str] = None,
        comment: Optional[str] = None,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ReviewAction:
        """review_actions 테이블에 새 레코드를 삽입한다."""
        import json
        sql = """
            INSERT INTO review_actions
                (document_id, version_id, action_type, from_status, to_status,
                 actor_id, actor_role, comment, reason, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, document_id, version_id, action_type, from_status, to_status,
                      actor_id, actor_role, comment, reason, metadata, created_at
        """
        meta_json = json.dumps(metadata or {})
        with conn.cursor() as cur:
            cur.execute(sql, (
                document_id, version_id, action_type, from_status, to_status,
                actor_id, actor_role, comment, reason, meta_json,
            ))
            row = cur.fetchone()
        return _row_to_review_action(dict(row))

    def list_review_actions(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list[ReviewAction]:
        """특정 버전의 review_actions를 시간 순으로 반환한다."""
        sql = """
            SELECT id, document_id, version_id, action_type, from_status, to_status,
                   actor_id, actor_role, comment, reason, metadata, created_at
            FROM review_actions
            WHERE version_id = %s
            ORDER BY created_at ASC
        """
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
            rows = cur.fetchall()
        return [_row_to_review_action(dict(r)) for r in rows]

    # -----------------------------------------------------------------------
    # workflow_history
    # -----------------------------------------------------------------------

    def create_workflow_history(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
        from_status: str,
        to_status: str,
        action: str,
        actor_id: Optional[str] = None,
        actor_role: Optional[str] = None,
        comment: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> WorkflowHistory:
        """workflow_history 테이블에 새 이력 레코드를 삽입한다."""
        sql = """
            INSERT INTO workflow_history
                (document_id, version_id, from_status, to_status, action,
                 actor_id, actor_role, comment, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, document_id, version_id, from_status, to_status, action,
                      actor_id, actor_role, comment, reason, created_at
        """
        with conn.cursor() as cur:
            cur.execute(sql, (
                document_id, version_id, from_status, to_status, action,
                actor_id, actor_role, comment, reason,
            ))
            row = cur.fetchone()
        return _row_to_workflow_history(dict(row))

    def list_workflow_history(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[WorkflowHistory], int]:
        """문서(또는 특정 버전)의 워크플로 이력을 반환한다.

        Returns:
            (history_list, total_count)
        """
        where = "WHERE document_id = %s"
        params: list[Any] = [document_id]

        if version_id:
            where += " AND version_id = %s"
            params.append(version_id)

        count_sql = f"SELECT COUNT(*) FROM workflow_history {where}"
        list_sql = f"""
            SELECT id, document_id, version_id, from_status, to_status, action,
                   actor_id, actor_role, comment, reason, created_at
            FROM workflow_history
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """

        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total = cur.fetchone()["count"]
            cur.execute(list_sql, params + [limit, offset])
            rows = cur.fetchall()

        return [_row_to_workflow_history(dict(r)) for r in rows], total

    # -----------------------------------------------------------------------
    # change_logs
    # -----------------------------------------------------------------------

    def create_change_log(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        change_type: str,
        actor_id: Optional[str] = None,
        actor_role: Optional[str] = None,
        version_id: Optional[str] = None,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ChangeLog:
        """change_logs 테이블에 새 레코드를 삽입한다."""
        import json
        sql = """
            INSERT INTO change_logs
                (document_id, version_id, change_type, reason, actor_id, actor_role, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, document_id, version_id, change_type, reason, actor_id, actor_role,
                      metadata, created_at
        """
        meta_json = json.dumps(metadata or {})
        with conn.cursor() as cur:
            cur.execute(sql, (
                document_id, version_id, change_type, reason, actor_id, actor_role, meta_json,
            ))
            row = cur.fetchone()
        return _row_to_change_log(dict(row))


workflow_repository = WorkflowRepository()
