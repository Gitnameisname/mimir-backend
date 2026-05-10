"""
VaultImports persistence repository — S3 Phase 2 FG 2-6.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

from app.db.cursor_helpers import fetch_many_as, fetch_one_as
from app.models.vault_import import VaultImport, VaultImportStatus
from app.repositories.pagination import clamp_pagination

logger = logging.getLogger(__name__)


_COLS = (
    "id, owner_id, uploaded_filename, bytes_original, bytes_extracted, "
    "file_count, status, scope_profile_id, started_at, finished_at, report, created_at"
)


def _row_to_import(row: dict[str, Any]) -> VaultImport:
    return VaultImport(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        uploaded_filename=row["uploaded_filename"],
        bytes_original=int(row["bytes_original"] or 0),
        bytes_extracted=int(row["bytes_extracted"] or 0),
        file_count=int(row["file_count"] or 0),
        status=row["status"],
        scope_profile_id=str(row["scope_profile_id"]),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        report=row["report"] if isinstance(row["report"], dict) else (
            json.loads(row["report"]) if row.get("report") else {}
        ),
        created_at=row["created_at"],
    )


class VaultImportsRepository:
    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        uploaded_filename: str,
        scope_profile_id: str,
        bytes_original: int,
    ) -> VaultImport:
        sql = f"""
            INSERT INTO vault_imports
                (owner_id, uploaded_filename, scope_profile_id, bytes_original, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING {_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (owner_id, uploaded_filename, scope_profile_id, bytes_original),
            )
            row = cur.fetchone()
        return _row_to_import(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        import_id: str,
    ) -> Optional[VaultImport]:
        sql = f"SELECT {_COLS} FROM vault_imports WHERE id = %s"
        return fetch_one_as(conn, sql, (import_id,), lambda r: _row_to_import(dict(r)))

    def list_by_owner(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[VaultImport], int]:
        page_size, offset = clamp_pagination(page_size, page, max_limit=100, default_limit=20)
        total = fetch_one_as(
            conn,
            "SELECT COUNT(*)::INT AS cnt FROM vault_imports WHERE owner_id = %s",
            (owner_id,),
            lambda r: int(r["cnt"]),
        ) or 0
        sql = f"""
            SELECT {_COLS}
            FROM vault_imports
            WHERE owner_id = %s
            ORDER BY created_at DESC, id ASC
            LIMIT %s OFFSET %s
        """
        items = fetch_many_as(
            conn, sql, (owner_id, page_size, offset),
            lambda r: _row_to_import(dict(r)),
        )
        return items, total

    def update_status(
        self,
        conn: psycopg2.extensions.connection,
        *,
        import_id: str,
        status: VaultImportStatus,
        bytes_extracted: Optional[int] = None,
        file_count: Optional[int] = None,
        report: Optional[dict[str, Any]] = None,
        owner_id: Optional[str] = None,  # cancel 시 owner 강제용
    ) -> Optional[VaultImport]:
        sets = ["status = %s"]
        params: list[Any] = [status]
        if bytes_extracted is not None:
            sets.append("bytes_extracted = %s")
            params.append(bytes_extracted)
        if file_count is not None:
            sets.append("file_count = %s")
            params.append(file_count)
        if report is not None:
            sets.append("report = %s::jsonb")
            params.append(json.dumps(report))

        # 시작 / 종료 시각 자동 마킹
        if status == "running":
            sets.append("started_at = COALESCE(started_at, NOW())")
        elif status in ("succeeded", "failed", "cancelled"):
            sets.append("finished_at = NOW()")

        where = ["id = %s"]
        params.append(import_id)
        if owner_id is not None:
            where.append("owner_id = %s")
            params.append(owner_id)

        sql = f"""
            UPDATE vault_imports
            SET {', '.join(sets)}
            WHERE {' AND '.join(where)}
            RETURNING {_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_import(dict(row)) if row else None


vault_imports_repository = VaultImportsRepository()
