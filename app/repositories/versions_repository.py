"""
Versions persistence repository.

책임:
  - versions 테이블에 대한 SQL CRUD 실행
  - DB row(RealDictRow) → Version 도메인 모델 변환
  - document-version 관계 무결성 접근

설계 원칙:
  - router나 service는 SQL을 직접 작성하지 않는다.
  - 트랜잭션 경계는 get_db() 컨텍스트가 관리한다.
  - version_number는 문서별 순차 증가 (1-based). get_next_version_number로 계산.
"""

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

from app.models.version import Version

logger = logging.getLogger(__name__)


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
    ) -> Version:
        """새 버전 row를 생성하고 반환한다."""
        sql = """
            INSERT INTO versions
                (document_id, version_number, label, status, change_summary,
                 source, metadata, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING
                id, document_id, version_number, label, status, change_summary,
                source, metadata, created_by, created_at
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
                    json.dumps(metadata, ensure_ascii=False),
                    created_by,
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
        sql = """
            SELECT id, document_id, version_number, label, status, change_summary,
                   source, metadata, created_by, created_at
            FROM versions
            WHERE id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (version_id,))
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
        sort_field: str = "created_at",
        sort_dir: str = "DESC",
    ) -> tuple[list[Version], int]:
        """문서별 버전 목록과 전체 건수를 반환한다.

        sort_field, sort_dir은 whitelist에서만 허용한다.
        """
        _SORT_WHITELIST = {
            "created_at": "created_at",
            "version_number": "version_number",
        }
        col = _SORT_WHITELIST.get(sort_field, "created_at")
        direction = "DESC" if sort_dir.upper() == "DESC" else "ASC"

        offset = (page - 1) * page_size

        count_sql = "SELECT COUNT(*) AS total FROM versions WHERE document_id = %s"
        data_sql = f"""
            SELECT id, document_id, version_number, label, status, change_summary,
                   source, metadata, created_by, created_at
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


# 모듈 수준 싱글턴
versions_repository = VersionsRepository()
