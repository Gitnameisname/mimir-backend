"""Contributors 집계 repository — S3 Phase 3 FG 3-1.

audit_events + workflow_history + documents 의 4 카테고리 distinct actor 집계.

설계 원칙:
    - 본 repository 는 SQL 만 담당. user 표시명 머지 / actor_type 필터 / dedup 은 service 가 책임.
    - ACL 적용은 호출자 (service) — 본 모듈은 임의로 scope_profile_id 필터를 넣지 않음
      (S2 ⑤~⑦ 원칙: scope 하드코딩 금지).
    - 카테고리별 쿼리는 별 SQL 호출이지만 같은 connection 안에서 순차 실행.

함수 도서관: ``docs/함수도서관/backend.md`` §2-fg31 (FG 3-1 신설, application-domain repository 라
도서관 §0 등록 대상은 아님 — 도메인 helper 임을 명시).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import psycopg2.extensions

from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# Contributors 패널이 의존하는 audit event_type 8 종 (산출물 §2.2 / 2.3 / 2.4 참조).
EDITOR_EVENT_TYPES: tuple[str, ...] = (
    "document.created",
    "document.updated",
    "draft.updated",
    "draft.nodes_saved",
    "draft.discarded",
    "version.created",
    "version.restored",
)

VIEWER_EVENT_TYPE: str = "document.viewed"


class ContributorsRepository:
    """문서별 contributors 집계 쿼리 모음."""

    # ------------------------------------------------------------------
    # 작성자 (creator) — documents.created_by
    # ------------------------------------------------------------------

    def get_creator(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> Optional[dict[str, Any]]:
        """``{actor_id, last_activity_at}`` 또는 None.

        documents.created_by 가 NULL 이면 None.
        last_activity_at = documents.created_at.
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_by, created_at
                FROM documents
                WHERE id = %s
                """,
                (document_id,),
            )
            row = cur.fetchone()
            if not row or not row.get("created_by"):
                return None
            return {
                "actor_id": str(row["created_by"]),
                "last_activity_at": row["created_at"],
            }

    # ------------------------------------------------------------------
    # 편집자 (editors) — audit_events 의 편집 이벤트들
    # ------------------------------------------------------------------

    def list_editors(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``[{actor_id, actor_type, actor_role, last_activity_at}, ...]``.

        EDITOR_EVENT_TYPES 8 종에서 distinct actor_user_id.
        actor_user_id 가 NULL 인 행은 제외 (system / anonymous emit 케이스).
        since 가 있으면 occurred_at >= since 로 필터.
        결과 정렬: last_activity_at DESC.
        """
        params: list[Any] = [document_id, list(EDITOR_EVENT_TYPES)]
        since_clause = ""
        if since is not None:
            since_clause = "AND occurred_at >= %s"
            params.append(since)
        params.append(int(limit))

        sql = f"""
            SELECT
                actor_user_id  AS actor_id,
                actor_type,
                actor_role,
                MAX(occurred_at) AS last_activity_at
            FROM audit_events
            WHERE document_id = %s
              AND event_type = ANY(%s)
              AND actor_user_id IS NOT NULL
              {since_clause}
            GROUP BY actor_user_id, actor_type, actor_role
            ORDER BY MAX(occurred_at) DESC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # 승인자 (approvers) — workflow_history.to_status='published'
    # ------------------------------------------------------------------

    def list_approvers(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``[{actor_id, actor_role, last_activity_at}, ...]``.

        workflow_history 의 to_status='published' distinct actor.
        workflow_history 는 actor_type 컬럼이 없으므로 service 가 'user' 로 default.
        """
        params: list[Any] = [document_id]
        since_clause = ""
        if since is not None:
            since_clause = "AND created_at >= %s"
            params.append(since)
        params.append(int(limit))

        sql = f"""
            SELECT
                actor_id,
                actor_role,
                MAX(created_at) AS last_activity_at
            FROM workflow_history
            WHERE document_id = %s
              AND to_status = 'published'
              AND actor_id IS NOT NULL
              {since_clause}
            GROUP BY actor_id, actor_role
            ORDER BY MAX(created_at) DESC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # 최근 열람자 (viewers) — audit_events.event_type='document.viewed'
    # ------------------------------------------------------------------

    def list_viewers(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``[{actor_id, actor_type, actor_role, last_activity_at}, ...]``.

        VIEWER_EVENT_TYPE distinct actor. since=None 이면 호출자 service 에서 기본 30일 적용.
        """
        params: list[Any] = [document_id, VIEWER_EVENT_TYPE]
        since_clause = ""
        if since is not None:
            since_clause = "AND occurred_at >= %s"
            params.append(since)
        params.append(int(limit))

        sql = f"""
            SELECT
                actor_user_id  AS actor_id,
                actor_type,
                actor_role,
                MAX(occurred_at) AS last_activity_at
            FROM audit_events
            WHERE document_id = %s
              AND event_type = %s
              AND actor_user_id IS NOT NULL
              {since_clause}
            GROUP BY actor_user_id, actor_type, actor_role
            ORDER BY MAX(occurred_at) DESC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]


contributors_repository = ContributorsRepository()
