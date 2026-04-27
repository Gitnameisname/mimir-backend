"""Annotations + AnnotationMentions Repository — S3 Phase 3 FG 3-3.

책임:
    - annotations / annotation_mentions 테이블 CRUD
    - DB row → Annotation 도메인 모델 변환 (mentioned_user_ids 함께 채움)

설계:
    - 본 모듈은 SQL 만 담당. ACL / 멘션 파싱 / audit emit 은 service 책임.
    - mentioned_user_ids 는 list_for_document / get_by_id 에서 LEFT JOIN 으로 batch fetch.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

import psycopg2.extensions

from app.models.annotation import Annotation
from app.utils.json_utils import dumps_ko
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


_ANNOTATION_COLS = (
    "id, document_id, version_id, node_id, span_start, span_end, "
    "author_id, actor_type, content, status, resolved_at, resolved_by, "
    "parent_id, is_orphan, orphaned_at, created_at, updated_at"
)


def _row_to_annotation(row: dict[str, Any], mentioned: Optional[list[str]] = None) -> Annotation:
    return Annotation(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        version_id=str(row["version_id"]) if row.get("version_id") else None,
        node_id=str(row["node_id"]),
        span_start=row.get("span_start"),
        span_end=row.get("span_end"),
        author_id=row["author_id"],
        actor_type=row.get("actor_type", "user"),
        content=row["content"],
        status=row.get("status", "open"),
        resolved_at=row.get("resolved_at"),
        resolved_by=row.get("resolved_by"),
        parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
        is_orphan=bool(row.get("is_orphan", False)),
        orphaned_at=row.get("orphaned_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        mentioned_user_ids=mentioned or [],
    )


class AnnotationsRepository:
    """annotations + annotation_mentions 통합 접근."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_id: Optional[str],
        node_id: str,
        span_start: Optional[int],
        span_end: Optional[int],
        author_id: str,
        actor_type: str,
        content: str,
        parent_id: Optional[str] = None,
    ) -> Annotation:
        annotation_id = str(uuid4())
        now = utcnow()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO annotations (
                    id, document_id, version_id, node_id, span_start, span_end,
                    author_id, actor_type, content, status, parent_id,
                    is_orphan, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, false, %s, %s)
                RETURNING {_ANNOTATION_COLS}
                """,
                (
                    annotation_id, document_id, version_id, node_id,
                    span_start, span_end,
                    author_id, actor_type, content, parent_id,
                    now, now,
                ),
            )
            row = cur.fetchone()
        return _row_to_annotation(row, mentioned=[])

    def get_by_id(
        self, conn: psycopg2.extensions.connection, annotation_id: str,
    ) -> Optional[Annotation]:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ANNOTATION_COLS} FROM annotations WHERE id = %s",
                (annotation_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        mentioned = self._list_mentions(conn, [str(row["id"])]).get(str(row["id"]), [])
        return _row_to_annotation(row, mentioned=mentioned)

    def list_for_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        include_resolved: bool = True,
        include_orphans: bool = True,
        limit: int = 200,
    ) -> list[Annotation]:
        """문서 단위 annotation list. 부모-자식 평탄화 — 호출자가 트리화."""
        conditions = ["document_id = %s"]
        params: list[Any] = [document_id]
        if not include_resolved:
            conditions.append("status = 'open'")
        if not include_orphans:
            conditions.append("is_orphan = false")
        where = " AND ".join(conditions)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_ANNOTATION_COLS}
                FROM annotations
                WHERE {where}
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (*params, int(limit)),
            )
            rows = cur.fetchall()
        if not rows:
            return []
        ids = [str(r["id"]) for r in rows]
        mentions_map = self._list_mentions(conn, ids)
        return [_row_to_annotation(r, mentioned=mentions_map.get(str(r["id"]), [])) for r in rows]

    def update_content(
        self,
        conn: psycopg2.extensions.connection,
        annotation_id: str,
        new_content: str,
    ) -> Optional[Annotation]:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE annotations SET content = %s, updated_at = %s
                WHERE id = %s
                RETURNING {_ANNOTATION_COLS}
                """,
                (new_content, utcnow(), annotation_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        mentioned = self._list_mentions(conn, [str(row["id"])]).get(str(row["id"]), [])
        return _row_to_annotation(row, mentioned=mentioned)

    def set_status(
        self,
        conn: psycopg2.extensions.connection,
        annotation_id: str,
        *,
        status: str,
        resolved_by: Optional[str] = None,
    ) -> Optional[Annotation]:
        now = utcnow()
        if status == "resolved":
            sql = f"""
                UPDATE annotations
                SET status = 'resolved', resolved_at = %s, resolved_by = %s, updated_at = %s
                WHERE id = %s
                RETURNING {_ANNOTATION_COLS}
            """
            params = (now, resolved_by, now, annotation_id)
        else:  # 'open' (reopen)
            sql = f"""
                UPDATE annotations
                SET status = 'open', resolved_at = NULL, resolved_by = NULL, updated_at = %s
                WHERE id = %s
                RETURNING {_ANNOTATION_COLS}
            """
            params = (now, annotation_id)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            return None
        mentioned = self._list_mentions(conn, [str(row["id"])]).get(str(row["id"]), [])
        return _row_to_annotation(row, mentioned=mentioned)

    def delete(self, conn: psycopg2.extensions.connection, annotation_id: str) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM annotations WHERE id = %s", (annotation_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Anchoring / Orphan
    # ------------------------------------------------------------------

    def mark_orphans(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        live_node_ids: set[str],
    ) -> tuple[int, int]:
        """현재 snapshot 에 없는 node_id 의 annotation 을 orphan 으로 표시,
        다시 등장한 node_id 의 annotation 은 orphan 해제.

        Returns:
            (newly_orphaned_count, recovered_count)
        """
        live_list = list(live_node_ids) if live_node_ids else []
        newly_orphaned = 0
        recovered = 0
        with conn.cursor() as cur:
            # 1) 현재 orphan=false 인데 node_id 가 live 에 없음 → orphan 처리
            if live_list:
                cur.execute(
                    """
                    UPDATE annotations
                    SET is_orphan = true, orphaned_at = NOW(), updated_at = NOW()
                    WHERE document_id = %s
                      AND is_orphan = false
                      AND node_id <> ALL(%s::uuid[])
                    """,
                    (document_id, live_list),
                )
            else:
                # live 비어있음 → 모든 annotation 이 orphan
                cur.execute(
                    """
                    UPDATE annotations
                    SET is_orphan = true, orphaned_at = NOW(), updated_at = NOW()
                    WHERE document_id = %s AND is_orphan = false
                    """,
                    (document_id,),
                )
            newly_orphaned = cur.rowcount or 0

            # 2) 현재 orphan=true 인데 node_id 가 live 에 다시 등장 → 복구
            if live_list:
                cur.execute(
                    """
                    UPDATE annotations
                    SET is_orphan = false, orphaned_at = NULL, updated_at = NOW()
                    WHERE document_id = %s
                      AND is_orphan = true
                      AND node_id = ANY(%s::uuid[])
                    """,
                    (document_id, live_list),
                )
                recovered = cur.rowcount or 0

        return newly_orphaned, recovered

    # ------------------------------------------------------------------
    # Mentions
    # ------------------------------------------------------------------

    def replace_mentions(
        self,
        conn: psycopg2.extensions.connection,
        annotation_id: str,
        mentioned_user_ids: list[str],
    ) -> None:
        """annotation 의 멘션 집합을 통째로 교체."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM annotation_mentions WHERE annotation_id = %s",
                (annotation_id,),
            )
            if mentioned_user_ids:
                # bulk insert
                args = [(annotation_id, uid) for uid in set(mentioned_user_ids)]
                cur.executemany(
                    "INSERT INTO annotation_mentions (annotation_id, mentioned_user_id) "
                    "VALUES (%s, %s)",
                    args,
                )

    def _list_mentions(
        self, conn: psycopg2.extensions.connection, annotation_ids: list[str],
    ) -> dict[str, list[str]]:
        """annotation_id 리스트 → {annotation_id: [mentioned_user_id, ...]} 매핑."""
        if not annotation_ids:
            return {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT annotation_id, mentioned_user_id
                FROM annotation_mentions
                WHERE annotation_id = ANY(%s::uuid[])
                """,
                (annotation_ids,),
            )
            rows = cur.fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(str(r["annotation_id"]), []).append(r["mentioned_user_id"])
        return result


annotations_repository = AnnotationsRepository()
