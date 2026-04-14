"""
System Settings persistence repository (Phase 14-11).

책임:
  - system_settings 테이블 CRUD
  - 카테고리별 / 단건 조회 / 단건 업데이트
  - JSONB value 타입 보존 (Python 객체 ↔ JSONB)

서비스 레이어가 SQL을 직접 작성하지 않도록 추상화한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2.extensions

logger = logging.getLogger(__name__)


def _row_to_setting(row: dict[str, Any]) -> dict[str, Any]:
    """system_settings row → API 응답 dict.

    psycopg2의 JSONB는 자동으로 Python 객체로 디코딩되므로 그대로 반환한다.
    """
    return {
        "id": str(row["id"]),
        "category": row["category"],
        "key": row["key"],
        "value": row["value"],
        "description": row.get("description"),
        "updated_by": str(row["updated_by"]) if row.get("updated_by") else None,
        "updated_at": row["updated_at"],
    }


class SettingsRepository:
    """system_settings 테이블에 대한 CRUD 추상화."""

    # ---------------------------------------------------------------
    # 조회
    # ---------------------------------------------------------------

    def list_all(self, conn: psycopg2.extensions.connection) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, category, key, value, description, updated_by, updated_at
                FROM system_settings
                ORDER BY category, key
                """
            )
            return [_row_to_setting(r) for r in cur.fetchall()]

    def list_by_category(
        self, conn: psycopg2.extensions.connection, category: str
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, category, key, value, description, updated_by, updated_at
                FROM system_settings
                WHERE category = %s
                ORDER BY key
                """,
                (category,),
            )
            return [_row_to_setting(r) for r in cur.fetchall()]

    def get_one(
        self, conn: psycopg2.extensions.connection, category: str, key: str
    ) -> Optional[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, category, key, value, description, updated_by, updated_at
                FROM system_settings
                WHERE category = %s AND key = %s
                """,
                (category, key),
            )
            row = cur.fetchone()
            return _row_to_setting(row) if row else None

    def list_categories(self, conn: psycopg2.extensions.connection) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT category FROM system_settings ORDER BY category"
            )
            return [r["category"] for r in cur.fetchall()]

    # ---------------------------------------------------------------
    # 업데이트
    # ---------------------------------------------------------------

    def update_value(
        self,
        conn: psycopg2.extensions.connection,
        category: str,
        key: str,
        new_value: Any,
        updated_by: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """value 컬럼을 업데이트하고 변경된 row를 반환한다.

        존재하지 않는 (category, key) 조합이면 None을 반환한다.
        값은 JSON으로 직렬화되어 JSONB에 저장된다 (타입 보존).
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE system_settings
                SET value = %s::jsonb,
                    updated_by = %s,
                    updated_at = NOW()
                WHERE category = %s AND key = %s
                RETURNING id, category, key, value, description, updated_by, updated_at
                """,
                (json.dumps(new_value), updated_by, category, key),
            )
            row = cur.fetchone()
            return _row_to_setting(row) if row else None


settings_repository = SettingsRepository()
