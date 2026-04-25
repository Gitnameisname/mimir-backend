"""
Versions persistence repository.

책임:
  - versions 테이블에 대한 SQL CRUD 실행
  - DB row(RealDictRow) → Version 도메인 모델 변환
  - document-version 관계 무결성 접근

Phase 4 확장:
  - create(): parent_version_id, restored_from_version_id, *_snapshot 컬럼 지원
  - get_active_draft(): 문서의 현재 활성 Draft 조회
  - update_status(): 버전 상태 변경 (Draft→Published, Published→Superseded 등)
  - get_by_document_and_version_id(): document 소속 검증 포함 단건 조회
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import psycopg2.extensions

from app.models.version import Version
from app.utils.json_utils import dumps_ko
from app.repositories.pagination import paginate_page

logger = logging.getLogger(__name__)

# 조회 SELECT 컬럼 목록 (공통)
_SELECT_COLS = """
    id, document_id, version_number, label, status, change_summary,
    source, metadata, created_by, created_at,
    parent_version_id, restored_from_version_id,
    title_snapshot, summary_snapshot, metadata_snapshot, content_snapshot,
    published_by, published_at
"""


def _row_to_version(row: dict[str, Any]) -> Version:
    """DB row(RealDictRow) → Version 도메인 모델 변환."""
    return Version(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        version_number=row["version_number"],
        label=row.get("label"),
        status=row["status"],
        change_summary=row.get("change_summary"),
        source=row["source"],
        metadata=row["metadata"] if row["metadata"] is not None else {},
        created_by=row.get("created_by"),
        created_at=row["created_at"],
        parent_version_id=(
            str(row["parent_version_id"]) if row.get("parent_version_id") else None
        ),
        restored_from_version_id=(
            str(row["restored_from_version_id"])
            if row.get("restored_from_version_id") else None
        ),
        title_snapshot=row.get("title_snapshot"),
        summary_snapshot=row.get("summary_snapshot"),
        metadata_snapshot=row.get("metadata_snapshot"),
        content_snapshot=row.get("content_snapshot"),
        published_by=row.get("published_by"),
        published_at=row.get("published_at"),
    )


class VersionsRepository:
    """Versions 테이블 접근 repository."""

    def get_next_version_number(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> int:
        """해당 문서의 다음 version_number를 반환한다 (현재 max + 1, 없으면 1)."""
        sql = """
            SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number
            FROM versions
            WHERE document_id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()
        return row["next_number"]

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_number: int,
        label: Optional[str],
        status: str,
        change_summary: Optional[str],
        source: str,
        metadata: dict[str, Any],
        created_by: Optional[str],
        parent_version_id: Optional[str] = None,
        restored_from_version_id: Optional[str] = None,
        title_snapshot: Optional[str] = None,
        summary_snapshot: Optional[str] = None,
        metadata_snapshot: Optional[dict[str, Any]] = None,
        content_snapshot: Optional[dict[str, Any]] = None,
    ) -> Version:
        """새 버전 row를 생성하고 반환한다."""
        sql = f"""
            INSERT INTO versions
                (document_id, version_number, label, status, change_summary,
                 source, metadata, created_by,
                 parent_version_id, restored_from_version_id,
                 title_snapshot, summary_snapshot, metadata_snapshot, content_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    document_id,
                    version_number,
                    label,
                    status,
                    change_summary,
                    source,
                    dumps_ko(metadata),
                    created_by,
                    parent_version_id,
                    restored_from_version_id,
                    title_snapshot,
                    summary_snapshot,
                    dumps_ko(metadata_snapshot)
                    if metadata_snapshot is not None else None,
                    dumps_ko(content_snapshot)
                    if content_snapshot is not None else None,
                ),
            )
            row = cur.fetchone()
        return _row_to_version(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> Optional[Version]:
        """version_id로 단건 조회. 없으면 None."""
        sql = f"SELECT {_SELECT_COLS} FROM versions WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def get_by_document_and_version_id(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        version_id: str,
    ) -> Optional[Version]:
        """document 소속 검증 포함 단건 조회. 없거나 소속 불일치이면 None."""
        sql = f"""
            SELECT {_SELECT_COLS}
            FROM versions
            WHERE id = %s AND document_id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (version_id, document_id))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def get_active_draft(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> Optional[Version]:
        """문서의 현재 활성 Draft(status='draft')를 반환한다. 없으면 None."""
        sql = f"""
            SELECT {_SELECT_COLS}
            FROM versions
            WHERE document_id = %s AND status = 'draft'
            ORDER BY version_number DESC
            LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def get_current_published(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> Optional[Version]:
        """문서의 현재 Published 버전을 반환한다. 없으면 None.

        Document.current_published_version_id로 직접 조회하는 것이 더 빠르지만,
        포인터 없이도 조회 가능한 fallback으로 사용한다.
        """
        sql = f"""
            SELECT {_SELECT_COLS}
            FROM versions
            WHERE document_id = %s AND status = 'published'
            ORDER BY version_number DESC
            LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def update_status(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        *,
        status: str,
        published_by: Optional[str] = None,
        published_at: Optional[datetime] = None,
    ) -> Optional[Version]:
        """버전 상태를 업데이트한다. published 전환 시 published_by/at 기록."""
        set_clauses = ["status = %s"]
        params: list[Any] = [status]

        if published_by is not None:
            set_clauses.append("published_by = %s")
            params.append(published_by)
        if published_at is not None:
            set_clauses.append("published_at = %s")
            params.append(published_at)

        params.append(version_id)

        sql = f"""
            UPDATE versions
            SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING {_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def update_content(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        *,
        label: Optional[str] = None,
        change_summary: Optional[str] = None,
        title_snapshot: Optional[str] = None,
        summary_snapshot: Optional[str] = None,
        metadata_snapshot: Optional[dict[str, Any]] = None,
        content_snapshot: Optional[dict[str, Any]] = None,
    ) -> Optional[Version]:
        """Draft 버전의 본문/메타 스냅샷을 교체한다."""
        set_clauses: list[str] = []
        params: list[Any] = []

        if label is not None:
            set_clauses.append("label = %s")
            params.append(label)
        if change_summary is not None:
            set_clauses.append("change_summary = %s")
            params.append(change_summary)
        if title_snapshot is not None:
            set_clauses.append("title_snapshot = %s")
            params.append(title_snapshot)
        if summary_snapshot is not None:
            set_clauses.append("summary_snapshot = %s")
            params.append(summary_snapshot)
        if metadata_snapshot is not None:
            set_clauses.append("metadata_snapshot = %s")
            params.append(dumps_ko(metadata_snapshot))
        if content_snapshot is not None:
            set_clauses.append("content_snapshot = %s")
            params.append(dumps_ko(content_snapshot))

        if not set_clauses:
            return self.get_by_id(conn, version_id)

        params.append(version_id)
        sql = f"""
            UPDATE versions
            SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING {_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_version(dict(row))

    def list_by_document_id(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_field: str = "version_number",
        sort_dir: str = "DESC",
    ) -> tuple[list[Version], int]:
        """문서별 버전 목록과 전체 건수를 반환한다."""
        _SORT_WHITELIST = {
            "created_at": "created_at",
            "version_number": "version_number",
        }
        col = _SORT_WHITELIST.get(sort_field, "version_number")
        direction = "DESC" if sort_dir.upper() == "DESC" else "ASC"
        page, page_size, offset = paginate_page(page, page_size)

        count_sql = "SELECT COUNT(*) AS total FROM versions WHERE document_id = %s"
        data_sql = f"""
            SELECT {_SELECT_COLS}
            FROM versions
            WHERE document_id = %s
            ORDER BY {col} {direction}
            LIMIT %s OFFSET %s
        """
        with conn.cursor() as cur:
            cur.execute(count_sql, (document_id,))
            total = cur.fetchone()["total"]
            cur.execute(data_sql, (document_id, page_size, offset))
            rows = cur.fetchall()

        versions = [_row_to_version(dict(row)) for row in rows]
        return versions, total

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> bool:
        """버전 row를 삭제한다 (Draft discard 시 사용). 삭제 성공이면 True."""
        sql = "DELETE FROM versions WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
            return cur.rowcount > 0


# 모듈 수준 싱글턴
versions_repository = VersionsRepository()
