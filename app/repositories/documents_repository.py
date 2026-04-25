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
from typing import Any, Optional, Sequence

import psycopg2.extensions
import psycopg2.extras

from app.api.query.models import ParsedListQuery, SortOrder
from app.models.document import Document
from app.utils.json_utils import dumps_ko
from app.repositories.pagination import paginate_page

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
    # S3 Phase 2 FG 2-0 (2026-04-24): 명시적 scope_profile_id 필터 (admin 용)
    # 일반 viewer 의 ACL 강제는 `list(..., viewer_scope_profile_ids=...)` 파라미터로
    # 적용되며 query.filters 와 독립적으로 동작한다.
    "scope_profile_id": "scope_profile_id",
}

# 공용 SELECT 컬럼 목록 (_row_to_document 와 짝)
_DOCUMENT_SELECT_COLS = (
    "id, title, document_type, status, metadata, summary, "
    "created_by, updated_by, created_at, updated_at, "
    "current_draft_version_id, current_published_version_id, scope_profile_id"
)


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
        current_draft_version_id=(
            str(row["current_draft_version_id"])
            if row.get("current_draft_version_id") else None
        ),
        current_published_version_id=(
            str(row["current_published_version_id"])
            if row.get("current_published_version_id") else None
        ),
        scope_profile_id=(
            str(row["scope_profile_id"])
            if row.get("scope_profile_id") else None
        ),
    )


def _build_list_query(
    query: ParsedListQuery,
    *,
    viewer_scope_profile_ids: Optional[Sequence[str]] = None,
) -> tuple[str, list[Any], str, list[Any]]:
    """ParsedListQuery → (data_sql, data_params, count_sql, count_params) 반환.

    Args:
        query: 정규화된 list query (filter/sort/page)
        viewer_scope_profile_ids: FG 2-0 ACL 강제.
            - ``None``: viewer Scope 필터를 적용하지 않음 (관리자 또는 내부 호출)
            - ``[]``: 빈 Scope set — 결과 없음 (scope 없는 actor)
            - ``["<uuid>", ...]``: documents.scope_profile_id 가 해당 집합에 속하는 row 만

    FG 2-1 특별 필터:
      - ``collection=<id>`` — 해당 컬렉션에 속한 문서만 (collection_documents 조인)
      - ``folder=<id>`` — 해당 폴더에 배치된 문서만 (document_folder 조인)
      - ``folder=<id>&include_subfolders=true`` — path prefix 매칭으로 하위 폴더 포함

    SQL injection 방지:
      - WHERE 절 값은 psycopg2 파라미터(%s)로 처리
      - ORDER BY 컬럼명은 whitelist(_SORT_FIELD_MAP)에서만 선택
    """
    where_clauses: list[str] = []
    params: list[Any] = []

    # FG 2-1 특별 필터: query.filters 에서 미리 꺼내고 기본 루프에서 제외
    collection_id = query.filters.get("collection")
    folder_id = query.filters.get("folder")
    include_subfolders_raw = query.filters.get("include_subfolders")
    include_subfolders = str(include_subfolders_raw).lower() in {"1", "true", "yes"}
    # FG 2-1 UX 3차 (2026-04-24): 제목 부분 일치 검색어.
    #   - 앞뒤 공백 제거 후 빈 문자열이면 필터 미적용
    #   - ILIKE 사용 → 대소문자/한글 정규화 무시
    #   - `%` / `_` 를 이스케이프해 사용자 입력이 패턴 메타 문자로 해석되지 않도록 방어
    q_raw = query.filters.get("q")
    q_value: Optional[str] = None
    if isinstance(q_raw, str):
        stripped = q_raw.strip()
        if stripped:
            # ESCAPE '\\' 를 쓰므로 백슬래시도 이스케이프
            escaped = (
                stripped
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            q_value = f"%{escaped}%"

    # FG 2-2: 태그 필터 — 서버에서 즉시 정규화 (사용자 대소문자/공백 차이 흡수).
    tag_raw = query.filters.get("tag")
    tag_filter: Optional[str] = None
    if isinstance(tag_raw, str):
        from app.services.tag_rules import normalize_tag as _norm_tag
        tag_filter = _norm_tag(tag_raw)

    _SPECIAL_KEYS = {"collection", "folder", "include_subfolders", "q", "tag"}

    for field_name, value in query.filters.items():
        if field_name in _SPECIAL_KEYS:
            continue
        col = _FILTER_FIELD_MAP.get(field_name)
        if col is None:
            continue  # 알 수 없는 filter는 무시 (spec에서 이미 검증됨)
        where_clauses.append(f"{col} = %s")
        params.append(value)

    # q 필터 — 제목 부분 일치 (ILIKE, escape)
    if q_value is not None:
        where_clauses.append("title ILIKE %s ESCAPE '\\'")
        params.append(q_value)

    # FG 2-2: 태그 필터 — 해당 태그 이름을 가진 문서만 (subquery JOIN)
    if tag_filter:
        where_clauses.append(
            """id IN (
                SELECT dt.document_id
                FROM document_tags dt
                JOIN tags t ON t.id = dt.tag_id
                WHERE t.name_normalized = %s
            )"""
        )
        params.append(tag_filter)

    # collection 필터 — N:M subquery
    if collection_id:
        where_clauses.append(
            "id IN (SELECT document_id FROM collection_documents WHERE collection_id = %s)"
        )
        params.append(collection_id)

    # folder 필터 — N:1 subquery (+ 선택적 subfolders)
    if folder_id:
        if include_subfolders:
            # 해당 폴더 + 모든 하위 폴더에 배치된 문서
            # folders.path 가 prefix 매칭되는 폴더 id 전체 → document_folder 조인
            where_clauses.append(
                """id IN (
                    SELECT df.document_id
                    FROM document_folder df
                    JOIN folders f ON f.id = df.folder_id
                    WHERE f.path LIKE (
                        SELECT path || '%%' FROM folders WHERE id = %s
                    )
                )"""
            )
            params.append(folder_id)
        else:
            where_clauses.append(
                "id IN (SELECT document_id FROM document_folder WHERE folder_id = %s)"
            )
            params.append(folder_id)

    # S3 Phase 2 FG 2-0: viewer Scope 필터 강제
    #   None  → skip (admin / 내부 호출)
    #   []    → 결과 없음 (1=0)
    #   [...] → scope_profile_id IN (...)
    if viewer_scope_profile_ids is not None:
        ids = list(viewer_scope_profile_ids)
        if not ids:
            where_clauses.append("1 = 0")
        else:
            placeholders = ", ".join(["%s"] * len(ids))
            where_clauses.append(f"scope_profile_id IN ({placeholders})")
            params.extend(ids)

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
    page, page_size, offset = paginate_page(page, page_size)

    data_sql = f"""
        SELECT {_DOCUMENT_SELECT_COLS}
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
        scope_profile_id: Optional[str] = None,
    ) -> Document:
        """새 문서 생성.

        Args:
            scope_profile_id: S3 Phase 2 FG 2-0 — 문서의 Scope Profile 바인딩.
                값이 없으면 NULL 저장. 저장 직전 service 가 ActorContext 기준으로
                결정해 주입한다. Alembic backfill + NOT NULL 정책은 FG 2-0 후속에서
                사용자 승인 시점에 도입 (우선 NULL 허용).
        """
        sql = f"""
            INSERT INTO documents
                (title, document_type, status, metadata, summary,
                 created_by, updated_by, scope_profile_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_DOCUMENT_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    title,
                    document_type,
                    status,
                    dumps_ko(metadata),
                    summary,
                    created_by,
                    created_by,  # 생성 시 updated_by = created_by
                    scope_profile_id,
                ),
            )
            row = cur.fetchone()
        return _row_to_document(dict(row))

    def get_by_id(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
    ) -> Optional[Document]:
        """문서 단건 조회.

        Args:
            viewer_scope_profile_ids: FG 2-0 ACL 강제 (list 와 동일 규약).
                None=skip, []=없음, [...]=IN. 필터로 차단된 경우 None 반환(→ 404).
                존재 여부 유출 방지 (403 아니라 404).
        """
        params: list[Any] = [document_id]
        extra_where = ""
        if viewer_scope_profile_ids is not None:
            ids = list(viewer_scope_profile_ids)
            if not ids:
                extra_where = " AND 1 = 0"
            else:
                placeholders = ", ".join(["%s"] * len(ids))
                extra_where = f" AND scope_profile_id IN ({placeholders})"
                params.extend(ids)

        sql = f"""
            SELECT {_DOCUMENT_SELECT_COLS}
            FROM documents
            WHERE id = %s{extra_where}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_document(dict(row))

    def list(
        self,
        conn: psycopg2.extensions.connection,
        query: ParsedListQuery,
        *,
        viewer_scope_profile_ids: Optional[Sequence[str]] = None,
    ) -> tuple[list[Document], int]:
        """문서 목록과 전체 건수를 반환한다.

        Args:
            viewer_scope_profile_ids: FG 2-0 ACL 강제.
                None=skip (admin/내부), []=결과 없음, [...]=scope_profile_id IN (...)

        Returns:
            (documents, total_count)
        """
        data_sql, data_params, count_sql, count_params = _build_list_query(
            query,
            viewer_scope_profile_ids=viewer_scope_profile_ids,
        )

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
            params.append(dumps_ko(metadata))
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
            RETURNING {_DOCUMENT_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_document(dict(row))


    def update_version_pointers(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
        *,
        current_draft_version_id: Optional[str] = None,
        current_published_version_id: Optional[str] = None,
        clear_draft: bool = False,
        updated_by: Optional[str] = None,
    ) -> Optional[Document]:
        """current_draft_version_id / current_published_version_id 포인터를 업데이트한다.

        Args:
            clear_draft: True이면 current_draft_version_id를 NULL로 설정.
                         current_draft_version_id와 동시에 사용하지 않는다.
        """
        set_clauses: list[str] = ["updated_at = NOW()"]
        params: list[Any] = []

        if clear_draft:
            set_clauses.append("current_draft_version_id = NULL")
        elif current_draft_version_id is not None:
            set_clauses.append("current_draft_version_id = %s")
            params.append(current_draft_version_id)

        if current_published_version_id is not None:
            set_clauses.append("current_published_version_id = %s")
            params.append(current_published_version_id)

        if updated_by is not None:
            set_clauses.append("updated_by = %s")
            params.append(updated_by)

        params.append(document_id)

        sql = f"""
            UPDATE documents
            SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING {_DOCUMENT_SELECT_COLS}
        """
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_document(dict(row))


# 모듈 수준 싱글턴 (service에서 import해 사용)
documents_repository = DocumentsRepository()
