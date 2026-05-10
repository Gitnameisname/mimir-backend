"""
SavedViews persistence repository — S3 Phase 2 FG 2-5.

책임:
  - ``saved_views`` 테이블 CRUD
  - owner 본인 목록 조회 (`list_by_owner`)
  - owner 별 카운트 (`count_by_owner` — 상한 50 강제용)
  - 단건 조회 (`get_by_id` — 모든 viewer 가 정의 읽기 가능, owner_id 마스킹은 service 단)

설계 원칙:
  - SQL 직접 (psycopg2). 다른 repository (tags / document_links) 와 동일 패턴.
  - `filter` / `sort` 는 JSONB — Python dict / list 그대로 직렬화.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

from app.db.cursor_helpers import fetch_many_as, fetch_one_as
from app.models.saved_view import SavedView, SavedViewLayout
from app.repositories.pagination import clamp_pagination

logger = logging.getLogger(__name__)


_VIEW_COLS = (
    "id, owner_id, name, filter, sort, layout, "
    "include_tag_nodes, created_at, updated_at"
)


def _row_to_view(row: dict[str, Any]) -> SavedView:
    return SavedView(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        name=row["name"],
        filter=row["filter"] if isinstance(row["filter"], dict) else (
            json.loads(row["filter"]) if row.get("filter") else {}
        ),
        sort=row["sort"] if isinstance(row["sort"], list) else (
            json.loads(row["sort"]) if row.get("sort") else []
        ),
        layout=row["layout"],
        include_tag_nodes=bool(row.get("include_tag_nodes", False)),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SavedViewsRepository:
    """``saved_views`` 테이블 접근."""

    # ------------------------------------------------------------------
    # 쓰기 — INSERT / UPDATE / DELETE
    # ------------------------------------------------------------------

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        name: str,
        filter: dict[str, Any],
        sort: list[dict[str, Any]],
        layout: SavedViewLayout,
        include_tag_nodes: bool = False,
    ) -> SavedView:
        sql = f"""
            INSERT INTO saved_views (owner_id, name, filter, sort, layout, include_tag_nodes)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)
            RETURNING {_VIEW_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    owner_id,
                    name,
                    json.dumps(filter),
                    json.dumps(sort),
                    layout,
                    include_tag_nodes,
                ),
            )
            row = cur.fetchone()
        return _row_to_view(dict(row))

    def update(
        self,
        conn: psycopg2.extensions.connection,
        *,
        view_id: str,
        owner_id: str,
        name: Optional[str] = None,
        filter: Optional[dict[str, Any]] = None,
        sort: Optional[list[dict[str, Any]]] = None,
        layout: Optional[SavedViewLayout] = None,
        include_tag_nodes: Optional[bool] = None,
    ) -> Optional[SavedView]:
        """owner_id 검증을 WHERE 절에 포함 — 다른 사용자가 view_id 만 알아도 수정 못 함."""
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if filter is not None:
            sets.append("filter = %s::jsonb")
            params.append(json.dumps(filter))
        if sort is not None:
            sets.append("sort = %s::jsonb")
            params.append(json.dumps(sort))
        if layout is not None:
            sets.append("layout = %s")
            params.append(layout)
        if include_tag_nodes is not None:
            sets.append("include_tag_nodes = %s")
            params.append(include_tag_nodes)
        if not sets:
            return self.get_by_id(conn, view_id)
        sets.append("updated_at = NOW()")

        sql = f"""
            UPDATE saved_views
            SET {', '.join(sets)}
            WHERE id = %s AND owner_id = %s
            RETURNING {_VIEW_COLS}
        """
        params.extend([view_id, owner_id])
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_view(dict(row)) if row else None

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        *,
        view_id: str,
        owner_id: str,
    ) -> bool:
        """owner_id 검증 포함. 본인 view 가 아니면 False."""
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_views WHERE id = %s AND owner_id = %s",
                (view_id, owner_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 읽기
    # ------------------------------------------------------------------

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        view_id: str,
    ) -> Optional[SavedView]:
        sql = f"SELECT {_VIEW_COLS} FROM saved_views WHERE id = %s"
        return fetch_one_as(conn, sql, (view_id,), lambda r: _row_to_view(dict(r)))

    def list_by_owner(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[SavedView], int]:
        page_size, offset = clamp_pagination(page_size, page, max_limit=200, default_limit=50)

        # total
        total = fetch_one_as(
            conn,
            "SELECT COUNT(*)::INT AS cnt FROM saved_views WHERE owner_id = %s",
            (owner_id,),
            lambda r: int(r["cnt"]),
        ) or 0

        sql = f"""
            SELECT {_VIEW_COLS}
            FROM saved_views
            WHERE owner_id = %s
            ORDER BY updated_at DESC, id ASC
            LIMIT %s OFFSET %s
        """
        items = fetch_many_as(
            conn, sql, (owner_id, page_size, offset),
            lambda r: _row_to_view(dict(r)),
        )
        return items, total

    def count_by_owner(
        self,
        conn: psycopg2.extensions.connection,
        owner_id: str,
    ) -> int:
        """사용자당 상한 50 강제용 카운트."""
        return fetch_one_as(
            conn,
            "SELECT COUNT(*)::INT AS cnt FROM saved_views WHERE owner_id = %s",
            (owner_id,),
            lambda r: int(r["cnt"]),
        ) or 0


# 모듈 수준 싱글턴
saved_views_repository = SavedViewsRepository()
