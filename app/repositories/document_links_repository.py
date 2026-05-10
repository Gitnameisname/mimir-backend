"""
DocumentLinks persistence repository — S3 Phase 2 FG 2-3.

책임:
  - ``document_links`` 테이블에 대한 SQL CRUD
  - ``replace_for_document(conn, from_document_id, rows)`` — wikilink 동기화 시점에 호출
  - ``list_backlinks(conn, to_document_id, ...)`` — 역방향 조회 (viewer Scope 자동 필터)
  - ``list_outgoing(conn, from_document_id, ...)`` — 정방향 조회 (관리자/디버깅)
  - ``resolve_title_prefix(conn, q, viewer_scope_profile_ids)`` — ``/documents/resolve`` 자동완성
    (resolver 자체는 본 repository 의 헬퍼이지만, 매칭 정책은 ``wikilink_resolver`` 모듈이
    정본 — 본 메서드는 단순 prefix LIKE 만 제공)

설계 원칙:
  - ``replace_for_document`` 는 트랜잭션 내 DELETE → INSERT (FG 2-2 ``tags_repository``
    동일 패턴)
  - viewer Scope 필터: ``viewer_scope_profile_ids`` 는 keyword-only required (S2 ⑥
    Scope 하드코딩 금지)
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

import psycopg2.extensions

from app.db.cursor_helpers import fetch_many_as, fetch_one_as
from app.models.document_link import DocumentLink, ResolvedStatus
from app.repositories.pagination import clamp_pagination

logger = logging.getLogger(__name__)


_LINK_COLS = (
    "id, from_document_id, to_document_id, node_id, raw_text, "
    "resolved_status, created_at"
)


def _row_to_link(row: dict[str, Any]) -> DocumentLink:
    return DocumentLink(
        id=str(row["id"]),
        from_document_id=str(row["from_document_id"]),
        to_document_id=(
            str(row["to_document_id"]) if row.get("to_document_id") else None
        ),
        node_id=str(row["node_id"]),
        raw_text=row["raw_text"],
        resolved_status=row["resolved_status"],
        created_at=row["created_at"],
    )


class DocumentLinksRepository:
    """``document_links`` 테이블 접근."""

    # ------------------------------------------------------------------
    # 쓰기 — replace 패턴
    # ------------------------------------------------------------------

    def replace_for_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        from_document_id: str,
        rows: Iterable[tuple[Optional[str], str, str, ResolvedStatus]],
    ) -> int:
        """문서의 모든 wikilink 를 주어진 집합으로 **replace**.

        ``rows`` = iterable of ``(to_document_id, node_id, raw_text, resolved_status)``.
        ``to_document_id`` 는 missing/ambiguous 일 때 ``None``.

        Returns:
            INSERT 된 행 수.
        """
        rows_list = list(rows)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM document_links WHERE from_document_id = %s",
                (from_document_id,),
            )
            if not rows_list:
                return 0
            args = [
                cur.mogrify(
                    "(%s, %s, %s, %s, %s)",
                    (from_document_id, to_doc_id, node_id, raw_text, status),
                ).decode()
                for to_doc_id, node_id, raw_text, status in rows_list
            ]
            cur.execute(
                "INSERT INTO document_links "
                "(from_document_id, to_document_id, node_id, raw_text, resolved_status) "
                "VALUES " + ", ".join(args)
            )
        return len(rows_list)

    # ------------------------------------------------------------------
    # 읽기 — 역방향 (backlinks)
    # ------------------------------------------------------------------

    def list_backlinks(
        self,
        conn: psycopg2.extensions.connection,
        *,
        to_document_id: str,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """이 문서를 참조하는 (resolved) 문서 목록.

        반환 항목 dict:
          - link_id, from_document_id, from_document_title, node_id, raw_text, created_at

        ``viewer_scope_profile_ids`` 규약:
          None = 필터 skip (admin/내부), [] = 결과 없음, [ids] = IN
        """
        page_size, offset = clamp_pagination(page_size, page, max_limit=100, default_limit=20)

        where_parts = [
            "dl.to_document_id = %s",
            "dl.resolved_status = 'resolved'",
        ]
        params: list[Any] = [to_document_id]
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                where_parts.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                where_parts.append(f"d.scope_profile_id IN ({placeholders})")
                params.extend(ids)

        where_sql = " AND ".join(where_parts)

        # total count
        count_sql = f"""
            SELECT COUNT(*)::INT AS cnt
            FROM document_links dl
            JOIN documents d ON d.id = dl.from_document_id
            WHERE {where_sql}
        """
        total = fetch_one_as(conn, count_sql, params, lambda r: int(r["cnt"])) or 0

        # page
        sql = f"""
            SELECT dl.id AS link_id,
                   dl.from_document_id,
                   d.title AS from_document_title,
                   dl.node_id,
                   dl.raw_text,
                   dl.created_at
            FROM document_links dl
            JOIN documents d ON d.id = dl.from_document_id
            WHERE {where_sql}
            ORDER BY dl.created_at DESC, dl.id ASC
            LIMIT %s OFFSET %s
        """
        page_params = list(params) + [page_size, offset]
        items = fetch_many_as(
            conn,
            sql,
            page_params,
            lambda r: {
                "link_id": str(r["link_id"]),
                "from_document_id": str(r["from_document_id"]),
                "from_document_title": r["from_document_title"],
                "node_id": str(r["node_id"]),
                "raw_text": r["raw_text"],
                "created_at": r["created_at"],
            },
        )
        return items, total

    # ------------------------------------------------------------------
    # 읽기 — 정방향 (admin/디버깅)
    # ------------------------------------------------------------------

    def list_outgoing(
        self,
        conn: psycopg2.extensions.connection,
        *,
        from_document_id: str,
        limit: int = 200,
    ) -> list[DocumentLink]:
        """이 문서가 내보내는 링크 — admin 전용 / 디버깅용. ACL 필터 없음."""
        limit, _ = clamp_pagination(limit, 0, max_limit=500, default_limit=200)
        sql = f"""
            SELECT {_LINK_COLS}
            FROM document_links
            WHERE from_document_id = %s
            ORDER BY created_at ASC, id ASC
            LIMIT %s
        """
        return fetch_many_as(conn, sql, (from_document_id, limit), lambda r: _row_to_link(dict(r)))

    # ------------------------------------------------------------------
    # 자동완성 — 제목 prefix 매칭 (viewer Scope 필터)
    # ------------------------------------------------------------------

    def resolve_title_prefix(
        self,
        conn: psycopg2.extensions.connection,
        *,
        q: str,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """``[[`` 입력 시 호출되는 자동완성. NFC 정규화된 prefix LIKE.

        반환 항목 dict:
          - id (document_id), title, updated_at

        매칭 정책 (단일 일치 / 복수 일치 / 없음 분류) 은 ``wikilink_resolver`` 가 정본.
        본 메서드는 prefix 후보 list 만 제공.
        """
        limit, _ = clamp_pagination(limit, 0, max_limit=20, default_limit=10)
        where_parts = ["d.title ILIKE %s"]
        params: list[Any] = [f"{q}%"]
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                where_parts.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                where_parts.append(f"d.scope_profile_id IN ({placeholders})")
                params.extend(ids)

        sql = f"""
            SELECT d.id, d.title, d.updated_at
            FROM documents d
            WHERE {' AND '.join(where_parts)}
            ORDER BY d.updated_at DESC, LENGTH(d.title) ASC, d.title ASC
            LIMIT %s
        """
        params.append(limit)
        return fetch_many_as(
            conn,
            sql,
            params,
            lambda r: {
                "id": str(r["id"]),
                "title": r["title"],
                "updated_at": r["updated_at"],
            },
        )

    # ------------------------------------------------------------------
    # 매칭 후보 — resolver 가 사용 (정확한 title 일치)
    # ------------------------------------------------------------------

    def find_candidates_by_title(
        self,
        conn: psycopg2.extensions.connection,
        *,
        title: str,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        """정확한 title 일치 문서 후보 목록 — resolver 가 호출.

        반환: 후보 dict ``{id, title, updated_at}`` list.
        """
        where_parts = ["d.title = %s"]
        params: list[Any] = [title]
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                where_parts.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                where_parts.append(f"d.scope_profile_id IN ({placeholders})")
                params.extend(ids)

        sql = f"""
            SELECT d.id, d.title, d.updated_at
            FROM documents d
            WHERE {' AND '.join(where_parts)}
            ORDER BY d.updated_at DESC, LENGTH(d.title) ASC, d.title ASC
        """
        return fetch_many_as(
            conn,
            sql,
            params,
            lambda r: {
                "id": str(r["id"]),
                "title": r["title"],
                "updated_at": r["updated_at"],
            },
        )


# 모듈 수준 싱글턴
document_links_repository = DocumentLinksRepository()
