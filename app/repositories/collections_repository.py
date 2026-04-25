"""
Collections persistence repository — S3 Phase 2 FG 2-1.

책임:
  - collections 테이블에 대한 SQL CRUD
  - collection_documents N:M 연결 테이블 관리
  - DB row → Collection / CollectionDocument 도메인 모델 변환

설계 원칙:
  - router/service 는 SQL 을 직접 작성하지 않는다
  - 모든 DB 접근은 이 repository 를 통한다
  - **owner 검증 책임은 service 계층** — repository 는 충실히 쿼리 실행만

보안
----
이 테이블은 ACL 에 영향을 주지 않는다 (뷰 레이어). 다만 컬렉션 **안의 문서**
조회는 documents.scope_profile_id 로 필터되어야 하므로, 서비스 계층이 viewer
Scope set 을 전달해 JOIN 쿼리에서 차단한다.
"""

import logging
from typing import Any, Optional, Sequence

import psycopg2.extensions

from app.models.collection import Collection, CollectionDocument
from app.models.document import Document
from app.db.cursor_helpers import fetch_many_as

logger = logging.getLogger(__name__)


_COLLECTION_SELECT_COLS = (
    "id, owner_id, name, description, created_at, updated_at"
)


def _row_to_collection(row: dict[str, Any]) -> Collection:
    return Collection(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        name=row["name"],
        description=row.get("description"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        document_count=row.get("document_count"),
    )


class CollectionsRepository:
    """컬렉션 테이블 CRUD."""

    # ------------------------------------------------------------------
    # CRUD — collections
    # ------------------------------------------------------------------

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        owner_id: str,
        name: str,
        description: Optional[str] = None,
    ) -> Collection:
        sql = f"""
            INSERT INTO collections (owner_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING {_COLLECTION_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, (owner_id, name, description))
            row = cur.fetchone()
        return _row_to_collection(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        owner_id: Optional[str] = None,
    ) -> Optional[Collection]:
        """단건 조회. owner_id 가 주어지면 소유자 일치 검증도 함께 수행.

        owner 불일치 시 None 반환 (존재 유출 방지 → 서비스가 404 처리).
        """
        where_parts = ["id = %s"]
        params: list[Any] = [collection_id]
        if owner_id is not None:
            where_parts.append("owner_id = %s")
            params.append(owner_id)

        sql = f"""
            SELECT {_COLLECTION_SELECT_COLS}
            FROM collections
            WHERE {' AND '.join(where_parts)}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_collection(dict(row))

    def list_by_owner(
        self,
        conn: psycopg2.extensions.connection,
        owner_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        include_counts: bool = True,
    ) -> tuple[list[Collection], int]:
        """소유자의 컬렉션 목록 + 전체 건수.

        include_counts=True 이면 각 컬렉션의 문서 수를 함께 반환한다
        (N+1 방지용 LEFT JOIN GROUP BY 단일 쿼리).
        """
        count_sql = "SELECT COUNT(*) AS total FROM collections WHERE owner_id = %s"
        with conn.cursor() as cur:
            cur.execute(count_sql, (owner_id,))
            total = int(cur.fetchone()["total"])

        if include_counts:
            data_sql = f"""
                SELECT c.id, c.owner_id, c.name, c.description,
                       c.created_at, c.updated_at,
                       COUNT(cd.document_id)::INT AS document_count
                FROM collections c
                LEFT JOIN collection_documents cd ON cd.collection_id = c.id
                WHERE c.owner_id = %s
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                LIMIT %s OFFSET %s
            """
        else:
            data_sql = f"""
                SELECT {_COLLECTION_SELECT_COLS}
                FROM collections
                WHERE owner_id = %s
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
            """
        with conn.cursor() as cur:
            cur.execute(data_sql, (owner_id, limit, offset))
            rows = cur.fetchall()
        return [_row_to_collection(dict(r)) for r in rows], total

    def update(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Collection]:
        set_parts: list[str] = ["updated_at = NOW()"]
        params: list[Any] = []
        if name is not None:
            set_parts.append("name = %s")
            params.append(name)
        if description is not None:
            set_parts.append("description = %s")
            params.append(description)
        params.append(collection_id)

        sql = f"""
            UPDATE collections
            SET {', '.join(set_parts)}
            WHERE id = %s
            RETURNING {_COLLECTION_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_collection(dict(row))

    def delete(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
    ) -> bool:
        """컬렉션 삭제. collection_documents 는 ON DELETE CASCADE 로 함께 삭제."""
        with conn.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # collection_documents — N:M
    # ------------------------------------------------------------------

    def add_documents(
        self,
        conn: psycopg2.extensions.connection,
        *,
        collection_id: str,
        document_ids: Sequence[str],
    ) -> int:
        """문서를 컬렉션에 추가. 이미 들어있는 문서는 skip (ON CONFLICT DO NOTHING).

        Returns:
            실제 삽입된 row 수 (이미 있던 건은 제외).
        """
        if not document_ids:
            return 0
        inserted = 0
        with conn.cursor() as cur:
            for doc_id in document_ids:
                cur.execute(
                    """
                    INSERT INTO collection_documents (collection_id, document_id)
                    VALUES (%s, %s)
                    ON CONFLICT (collection_id, document_id) DO NOTHING
                    """,
                    (collection_id, doc_id),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return inserted

    def remove_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        collection_id: str,
        document_id: str,
    ) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM collection_documents
                WHERE collection_id = %s AND document_id = %s
                """,
                (collection_id, document_id),
            )
            return cur.rowcount > 0

    def list_collection_ids_for_document(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        owner_id: str,
    ) -> list[str]:
        """해당 문서를 포함하고 있는 **해당 owner 소유** 의 컬렉션 id 목록.

        다른 owner 가 문서를 자기 컬렉션에 담아도 여기선 반환하지 않는다
        (문서 상세 화면의 'in_collection_ids' 는 본인 맥락만 표시).
        """
        sql = """
            SELECT cd.collection_id
            FROM collection_documents cd
            JOIN collections c ON c.id = cd.collection_id
            WHERE cd.document_id = %s AND c.owner_id = %s
            ORDER BY cd.added_at DESC
        """
        return fetch_many_as(conn, sql, (document_id, owner_id), lambda r: str(r["collection_id"]))

    def list_document_ids(
        self,
        conn: psycopg2.extensions.connection,
        collection_id: str,
        *,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
    ) -> list[str]:
        """컬렉션 내 문서 id 목록 (viewer Scope 로 필터).

        FG 2-0 ACL 준수: viewer Scope 밖의 문서는 목록에서 제외.
        """
        where_parts = ["cd.collection_id = %s"]
        params: list[Any] = [collection_id]
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                where_parts.append("1 = 0")
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                where_parts.append(f"d.scope_profile_id IN ({placeholders})")
                params.extend(ids)

        sql = f"""
            SELECT cd.document_id
            FROM collection_documents cd
            JOIN documents d ON d.id = cd.document_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY cd.position ASC, cd.added_at DESC
        """
        return fetch_many_as(conn, sql, params, lambda r: str(r["document_id"]))


# 모듈 수준 싱글턴
collections_repository = CollectionsRepository()
