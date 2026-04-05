"""
Documents persistence repository.

책임:
  - documents 테이블에 대한 SQL CRUD 실행
  - DB row(RealDictRow) → Document 도메인 모델 변환
  - service 레이어가 전달하는 ParsedListQuery 기반 동적 쿼리 생성

설계 원칙:
  - router나 service는 SQL을 직접 작성하지 않는다.
  - 모든 DB 접근은 이 repository를 통한다.
  - list query 결과(sort/filter/pagination)를 WHERE/ORDER BY/LIMIT으로 변환한다.
  - 이후 tenant_scope / audit_logging / versions join은 이 계층에서 확장한다.
"""

import json
import logging
from typing import Any, Optional

import psycopg2.extensions
import psycopg2.extras

from app.api.query.models import ParsedListQuery, SortOrder
from app.models.document import Document

logger = logging.getLogger(__name__)

# sort 필드 → DB 컬럼명 매핑 (SQL injection 방지용 whitelist)
_SORT_FIELD_MAP: dict[str, str] = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "title": "title",
    "status": "status",
}

# filter 필드 → DB 컬럼명 매핑
_FILTER_FIELD_MAP: dict[str, str] = {
    "status": "status",
    "document_type": "document_type",
    "owner_id": "created_by",  # owner_id query param → created_by 컬럼
}


def _row_to_document(row: dict[str, Any]) -> Document:
    """DB row(RealDictRow) → Document 도메인 모델 변환."""
    return Document(
        id=str(row["id"]),
        title=row["title"],
        document_type=row["document_type"],
        status=row["status"],
        metadata=row["metadata"] if row["metadata"] is not None else {},
        summary=row.get("summary"),
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _build_list_query(
    query: ParsedListQuery,
) -> tuple[str, list[Any], str, list[Any]]:
    """ParsedListQuery → (data_sql, data_params, count_sql, count_params) 반환.

    SQL injection 방지:
      - WHERE 절 값은 psycopg2 파라미터(%s)로 처리
      - ORDER BY 컬럼명은 whitelist(_SORT_FIELD_MAP)에서만 선택
    """
    where_clauses: list[str] = []
    params: list[Any] = []

    for field_name, value in query.filters.items():
        col = _FILTER_FIELD_MAP.get(field_name)
        if col is None:
            continue  # 알 수 없는 filter는 무시 (spec에서 이미 검증됨)
        where_clauses.append(f"{col} = %s")
        params.append(value)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    # ORDER BY
    order_clauses: list[str] = []
    for sort_order in query.sort_orders:
        col = _SORT_FIELD_MAP.get(sort_order.field)
        if col is None:
            continue
        direction = "ASC" if sort_order.direction == "asc" else "DESC"
        order_clauses.append(f"{col} {direction}")
    if not order_clauses:
        order_clauses = ["created_at DESC"]  # 기본 정렬
    order_sql = "ORDER BY " + ", ".join(order_clauses)

    # LIMIT / OFFSET
    page = query.page if query.page and query.page >= 1 else 1
    page_size = query.page_size if query.page_size and query.page_size >= 1 else 20
    offset = (page - 1) * page_size

    data_sql = f"""
        SELECT id, title, document_type, status, metadata, summary,
               created_by, updated_by, created_at, updated_at
        FROM documents
        {where_sql}
        {order_sql}
        LIMIT %s OFFSET %s
    """
    data_params = params + [page_size, offset]

    count_sql = f"SELECT COUNT(*) AS total FROM documents {where_sql}"
    count_params = list(params)

    return data_sql, data_params, count_sql, count_params


class DocumentsRepository:
    """Documents 테이블 접근 repository.

    인스턴스는 DocumentsService에서 생성해 사용한다.
    각 메서드는 호출자가 전달한 conn을 사용한다 — 트랜잭션 경계는 get_db() 컨텍스트가 관리.
    """

    def create(
        self,
        conn: psycopg2.extensions.connection,
        *,
        title: str,
        document_type: str,
        status: str,
        metadata: dict[str, Any],
        summary: Optional[str],
        created_by: Optional[str],
    ) -> Document:
        sql = """
            INSERT INTO documents
                (title, document_type, status, metadata, summary, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING
                id, title, document_type, status, metadata, summary,
                created_by, updated_by, created_at, updated_at
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    title,
                    document_type,
                    status,
                    json.dumps(metadata, ensure_ascii=False),
                    summary,
                    created_by,
                    created_by,  # 생성 시 updated_by = created_by
                ),
            )
            row = cur.fetchone()
        return _row_to_document(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> Optional[Document]:
        sql = """
            SELECT id, title, document_type, status, metadata, summary,
                   created_by, updated_by, created_at, updated_at
            FROM documents
            WHERE id = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_document(dict(row))

    def list(
        self,
        conn: psycopg2.extensions.connection,
        query: ParsedListQuery,
    ) -> tuple[list[Document], int]:
        """문서 목록과 전체 건수를 반환한다.

        Returns:
            (documents, total_count)
        """
        data_sql, data_params, count_sql, count_params = _build_list_query(query)

        with conn.cursor() as cur:
            cur.execute(count_sql, count_params)
            total = cur.fetchone()["total"]

            cur.execute(data_sql, data_params)
            rows = cur.fetchall()

        documents = [_row_to_document(dict(row)) for row in rows]
        return documents, total

    def update(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        summary: Optional[str] = None,
        updated_by: Optional[str] = None,
    ) -> Optional[Document]:
        """명시된 필드만 부분 업데이트한다 (partial update).

        변경할 필드가 없으면 현재 문서를 그대로 반환한다.
        문서가 존재하지 않으면 None 반환.
        """
        set_clauses: list[str] = ["updated_at = NOW()"]
        params: list[Any] = []

        if title is not None:
            set_clauses.append("title = %s")
            params.append(title)
        if status is not None:
            set_clauses.append("status = %s")
            params.append(status)
        if metadata is not None:
            set_clauses.append("metadata = %s")
            params.append(json.dumps(metadata, ensure_ascii=False))
        if summary is not None:
            set_clauses.append("summary = %s")
            params.append(summary)
        if updated_by is not None:
            set_clauses.append("updated_by = %s")
            params.append(updated_by)

        params.append(document_id)

        sql = f"""
            UPDATE documents
            SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING
                id, title, document_type, status, metadata, summary,
                created_by, updated_by, created_at, updated_at
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_document(dict(row))


# 모듈 수준 싱글턴 (service에서 import해 사용)
documents_repository = DocumentsRepository()
