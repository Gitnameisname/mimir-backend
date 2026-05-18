"""Notifications Service — S3 Phase 3 FG 3-3.

최소 in-app 알림 시스템.

정책:
    - rate-limit: per (author_id, recipient_id) 분당 5건. 초과 시 silently skip.
    - per-user 생성 30 req/min 은 REST 라우터에서 별도 dependency 로 적용.
    - 본 라운드는 in-app polling 만. 이메일 / 푸시는 S4 범위.

함수 도서관: ``docs/함수도서관/backend.md`` §1.7-fg33-notifications (FG 3-3 신설).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import psycopg2.extensions

from app.api.auth.models import ActorContext
from app.models.annotation import Notification
from app.repositories.notifications_repository import notifications_repository

logger = logging.getLogger(__name__)


__all__ = [
    "notifications_service",
    "NotificationsService",
    "DEFAULT_MENTION_RATE_LIMIT_PER_MIN",
]


DEFAULT_MENTION_RATE_LIMIT_PER_MIN: int = 5


def _mention_rate_limit() -> int:
    raw = os.environ.get("ANNOTATION_MENTION_RATE_LIMIT_PER_MIN")
    if not raw:
        return DEFAULT_MENTION_RATE_LIMIT_PER_MIN
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MENTION_RATE_LIMIT_PER_MIN
    return max(0, value)


class NotificationsService:
    """In-app 알림 enqueue / 조회 / 읽음 처리."""

    def enqueue_mention(
        self,
        conn: psycopg2.extensions.connection,
        *,
        author_id: str,
        recipient_id: str,
        annotation_id: str,
        document_id: str,
        snippet: str,
    ) -> Optional[Notification]:
        """annotation.mention 알림 1 건 enqueue (rate-limit 적용).

        - author_id == recipient_id (자기 자신 멘션) → skip.
        - rate-limit 초과 → skip.
        - 정상 → notifications row 생성.

        Returns:
            생성된 Notification 또는 None (skip).
        """
        if not author_id or not recipient_id:
            return None
        if author_id == recipient_id:
            return None  # self-mention skip

        # rate-limit
        limit = _mention_rate_limit()
        if limit > 0:
            recent = notifications_repository.count_recent_per_pair(
                conn,
                author_id=author_id,
                recipient_id=recipient_id,
                within_seconds=60,
            )
            if recent >= limit:
                logger.info(
                    "notifications: mention rate-limit hit (author=%s → recipient=%s, recent=%d, limit=%d)",
                    author_id, recipient_id, recent, limit,
                )
                return None

        # S3 Phase 6 FG 6-3 (2026-05-18): snippet 도 응답에 그대로 노출되므로
        # read-시점 sanitize 와 같은 정규화를 enqueue 단계에서 미리 적용 (저장 정합성).
        from app.utils.content_sanitizer import sanitize_for_response
        return notifications_repository.enqueue(
            conn,
            user_id=recipient_id,
            kind="annotation.mention",
            payload={
                "author_id": author_id,
                "annotation_id": annotation_id,
                "document_id": document_id,
                "snippet": sanitize_for_response((snippet or "")[:200]),
            },
        )

    def list_for_user(
        self,
        conn: psycopg2.extensions.connection,
        actor: ActorContext,
        *,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[Notification]:
        """현재 actor 본인의 알림 list. 다른 사용자 알림 조회 금지."""
        if not actor or not actor.is_authenticated or not actor.actor_id:
            return []
        return notifications_repository.list_for_user(
            conn,
            user_id=actor.actor_id,
            unread_only=unread_only,
            limit=limit,
        )

    def mark_read(
        self,
        conn: psycopg2.extensions.connection,
        actor: ActorContext,
        notification_ids: list[str],
    ) -> int:
        """본인 알림만 읽음 처리. 다른 사용자 알림은 영향 없음 (SQL 안 user_id 필터)."""
        if not actor or not actor.is_authenticated or not actor.actor_id:
            return 0
        return notifications_repository.mark_read(
            conn,
            user_id=actor.actor_id,
            notification_ids=notification_ids or [],
        )

    def count_unread(
        self,
        conn: psycopg2.extensions.connection,
        actor: ActorContext,
    ) -> int:
        if not actor or not actor.is_authenticated or not actor.actor_id:
            return 0
        return notifications_repository.count_unread(conn, actor.actor_id)


notifications_service = NotificationsService()
