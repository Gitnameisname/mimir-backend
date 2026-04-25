"""
Tags persistence repository — S3 Phase 2 FG 2-2.

책임:
  - tags / document_tags 테이블에 대한 SQL CRUD
  - 자동완성 (q prefix + usage 빈도) / popular 쿼리
  - `rebuild_tags_for_document` 호출부 (snapshot_sync_service 연쇄)

설계 원칙:
  - `replace_for_document(conn, document_id, assignments)` 는 해당 문서 분만
    DELETE → INSERT (트랜잭션 내)
  - 태그 upsert 는 ON CONFLICT (name_normalized) DO NOTHING
  - 사용 빈도는 `document_tags` 의 COUNT(*) GROUP BY 로 실시간 (캐시 없음 —
    태그 수가 폭발하는 Phase 3 이후 검토)
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

import psycopg2.extensions

from app.db.cursor_helpers import fetch_many_as
from app.models.tag import Tag
from app.repositories.pagination import clamp_pagination

logger = logging.getLogger(__name__)


_TAG_COLS = "id, name_normalized, created_at"


def _row_to_tag(row: dict[str, Any]) -> Tag:
    return Tag(
        id=str(row["id"]),
        name_normalized=row["name_normalized"],
        created_at=row["created_at"],
        usage_count=(
            int(row["usage_count"])
            if row.get("usage_count") is not None else None
        ),
    )


class TagsRepository:
    """태그 전역 풀 + document_tags 연결 테이블 접근."""

    # ------------------------------------------------------------------
    # tags — 전역 풀
    # ------------------------------------------------------------------

    def upsert_many(
        self,
        conn: psycopg2.extensions.connection,
        names_normalized: Sequence[str],
    ) -> dict[str, str]:
        """이름(정규화된) 집합을 upsert 하고 ``{name: tag_id}`` 매핑 반환.

        기존에 있던 태그는 id 가 유지되며, 새 태그는 신규 UUID 로 생성.
        """
        if not names_normalized:
            return {}
        result: dict[str, str] = {}
        with conn.cursor() as cur:
            for name in set(names_normalized):
                cur.execute(
                    """
                    INSERT INTO tags (name_normalized)
                    VALUES (%s)
                    ON CONFLICT (name_normalized) DO UPDATE
                        SET name_normalized = EXCLUDED.name_normalized
                    RETURNING id
                    """,
                    (name,),
                )
                row = cur.fetchone()
                if row:
                    result[name] = str(row["id"])
        return result

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        tag_id: str,
    ) -> Optional[Tag]:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_TAG_COLS} FROM tags WHERE id = %s",
                (tag_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_tag(dict(row))

    def search_prefix(
        self,
        conn: psycopg2.extensions.connection,
        *,
        q: Optional[str],
        limit: int = 20,
    ) -> list[Tag]:
        """자동완성 — 정규화된 prefix 매칭 + usage_count 내림차순.

        q 가 None / 빈 문자열이면 전체 태그 중 인기순 상위 N (popular 동작).
        """
        # 도서관 §1.9 BE-G5 (2026-04-25): clamp_pagination 위임 (offset 미사용)
        limit, _ = clamp_pagination(limit, 0, max_limit=100, default_limit=20)
        where = ""
        params: list[Any] = []
        if q:
            # q 는 서비스 레이어에서 정규화되어 들어온다고 가정
            where = "WHERE t.name_normalized LIKE %s"
            params.append(f"{q}%")
        sql = f"""
            SELECT t.id, t.name_normalized, t.created_at,
                   COUNT(dt.document_id)::INT AS usage_count
            FROM tags t
            LEFT JOIN document_tags dt ON dt.tag_id = t.id
            {where}
            GROUP BY t.id
            ORDER BY usage_count DESC NULLS LAST, t.name_normalized ASC
            LIMIT %s
        """
        params.append(limit)
        # 도서관 §1.8 BE-G5 (2026-04-25): fetch_many_as 위임
        return fetch_many_as(conn, sql, params, lambda r: _row_to_tag(dict(r)))

    def popular(
        self,
        conn: psycopg2.extensions.connection,
        *,
        limit: int = 50,
        min_usage: int = 1,
    ) -> list[Tag]:
        """사용 빈도 상위 태그. ``min_usage`` 이상만."""
        # 도서관 §1.9 BE-G5 (2026-04-25): clamp_pagination 위임 (offset 미사용)
        limit, _ = clamp_pagination(limit, 0, max_limit=200, default_limit=50)
        sql = """
            SELECT t.id, t.name_normalized, t.created_at,
                   COUNT(dt.document_id)::INT AS usage_count
            FROM tags t
            JOIN document_tags dt ON dt.tag_id = t.id
            GROUP BY t.id
            HAVING COUNT(dt.document_id) >= %s
            ORDER BY usage_count DESC, t.name_normalized ASC
            LIMIT %s
        """
        return fetch_many_as(conn, sql, (min_usage, limit), lambda r: _row_to_tag(dict(r)))

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        tag_id: str,
    ) -> bool:
        """관리자 전용 전역 삭제. document_tags 는 ON DELETE CASCADE 로 함께 정리."""
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tags WHERE id = %s", (tag_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # document_tags — 문서 연결
    # ------------------------------------------------------------------

    def replace_for_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        assignments: Iterable[tuple[str, str]],
    ) -> None:
        """문서의 모든 태그 연결을 주어진 집합으로 **replace**.

        ``assignments`` = iterable of ``(tag_id, source)``.
        기존 행은 모두 삭제되고 새로운 행이 INSERT 된다.
        """
        rows = list(assignments)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM document_tags WHERE document_id = %s",
                (document_id,),
            )
            if rows:
                args = [
                    cur.mogrify("(%s, %s, %s)", (document_id, tag_id, source)).decode()
                    for tag_id, source in rows
                ]
                cur.execute(
                    "INSERT INTO document_tags (document_id, tag_id, source) VALUES "
                    + ", ".join(args)
                )

    def list_for_document(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> list[tuple[Tag, str]]:
        """문서에 달린 태그 목록 ``(Tag, source)``."""
        sql = """
            SELECT t.id, t.name_normalized, t.created_at, dt.source,
                   NULL::INT AS usage_count
            FROM document_tags dt
            JOIN tags t ON t.id = dt.tag_id
            WHERE dt.document_id = %s
            ORDER BY t.name_normalized ASC
        """
        return fetch_many_as(conn, sql, (document_id,), lambda r: (_row_to_tag(dict(r)), r["source"]))

    def document_ids_for_tag(
        self,
        conn: psycopg2.extensions.connection,
        *,
        tag_name_normalized: str,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
    ) -> list[str]:
        """특정 태그를 가진 문서 id 목록 — **viewer Scope 자동 필터**.

        ``viewer_scope_profile_ids`` 규약:
          None = 필터 skip (admin/내부), [] = 결과 없음, [ids] = IN
        """
        where_parts = ["t.name_normalized = %s"]
        params: list[Any] = [tag_name_normalized]
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                where_parts.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                where_parts.append(f"d.scope_profile_id IN ({placeholders})")
                params.extend(ids)

        sql = f"""
            SELECT dt.document_id
            FROM document_tags dt
            JOIN tags t ON t.id = dt.tag_id
            JOIN documents d ON d.id = dt.document_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY dt.created_at DESC
        """
        return fetch_many_as(conn, sql, params, lambda r: str(r["document_id"]))


# 모듈 수준 싱글턴
tags_repository = TagsRepository()
