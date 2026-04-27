"""Notifications Repository — S3 Phase 3 FG 3-3.

최소 in-app 알림 시스템. S2 에 알림 시스템 부재로 본 FG 가 신설.

설계:
    - 본 모듈은 SQL 만 담당. rate-limit / payload 구성 / fan-out 은 service 책임.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

import psycopg2.extensions

from app.models.annotation import Notification
from app.utils.json_utils import dumps_ko, loads_maybe
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


_NOTIFICATION_COLS = "id, user_id, kind, payload, read_at, created_at"


def _row_to_notification(row: dict[str, Any]) -> Notification:
    raw_payload = row.get("payload")
    payload: dict
    if raw_payload is None:
        payload = {}
    else:
        parsed = loads_maybe(raw_payload)
        payload = parsed if isinstance(parsed, dict) else {}
    return Notification(
        id=str(row["id"]),
        user_id=row["user_id"],
        kind=row["kind"],
        payload=payload,
        read_at=row.get("read_at"),
        created_at=row["created_at"],
    )


class NotificationsRepository:
    def enqueue(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        kind: str,
        payload: dict,
    ) -> Notification:
        nid = str(uuid4())
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO notifications (id, user_id, kind, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING {_NOTIFICATION_COLS}
                """,
                (nid, user_id, kind, dumps_ko(payload or {})),
            )
            row = cur.fetchone()
        return _row_to_notification(row)

    def list_for_user(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        *,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[Notification]:
        if unread_only:
            sql = f"""
                SELECT {_NOTIFICATION_COLS}
                FROM notifications
                WHERE user_id = %s AND read_at IS NULL
                ORDER BY created_at DESC LIMIT %s
            """
        else:
            sql = f"""
                SELECT {_NOTIFICATION_COLS}
                FROM notifications
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s
            """
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, int(limit)))
            rows = cur.fetchall()
        return [_row_to_notification(r) for r in rows]

    def mark_read(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        notification_ids: list[str],
    ) -> int:
        if not notification_ids:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notifications
                SET read_at = NOW()
                WHERE user_id = %s
                  AND id = ANY(%s::uuid[])
                  AND read_at IS NULL
                """,
                (user_id, notification_ids),
            )
            return cur.rowcount or 0

    def count_unread(
        self, conn: psycopg2.extensions.connection, user_id: str,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM notifications WHERE user_id = %s AND read_at IS NULL",
                (user_id,),
            )
            row = cur.fetchone()
        return int(row["c"]) if row else 0

    def count_recent_per_pair(
        self,
        conn: psycopg2.extensions.connection,
        *,
        author_id: str,
        recipient_id: str,
        within_seconds: int,
    ) -> int:
        """rate-limit 용 — 최근 N 초 내 같은 (author, recipient) 알림 건수."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS c
                FROM notifications
                WHERE user_id = %s
                  AND created_at > NOW() - (%s * INTERVAL '1 second')
                  AND payload->>'author_id' = %s
                """,
                (recipient_id, int(within_seconds), author_id),
            )
            row = cur.fetchone()
        return int(row["c"]) if row else 0


notifications_repository = NotificationsRepository()
