"""
검색 서비스 — FTS + pgvector 하이브리드 검색.

설계 원칙:
  - 검색 레이어 추상화: FTS (mode=fts, 기본) 및 하이브리드 (mode=hybrid) 지원.
  - HybridSearchProvider: FTS 점수 + 벡터 유사도를 RRF(k=60)로 통합.
  - 권한 우선: 검색 결과는 요청자의 권한 범위 내에서만 반환.
  - API 계약 유지: mode 파라미터 추가 외 기존 Phase 8 계약 동일.
  - DocumentType-aware: 타입별 검색 가중치를 document_types 테이블 설정에서 읽어옴.
  - 스니펫: ts_headline()로 키워드 하이라이팅 포함 스니펫 반환.
"""

import logging
from datetime import datetime
from typing import Optional

import psycopg2.extensions
import psycopg2.extras

from app.schemas.search import (
    DocumentSearchResult,
    DocumentSearchResponse,
    DocumentSnippet,
    IndexStatsEntry,
    NodeBreadcrumb,
    NodeSearchResponse,
    NodeSearchResult,
    SearchIndexStats,
    SearchPagination,
    UnifiedSearchResponse,
)

logger = logging.getLogger(__name__)

_ADMIN_ROLES: frozenset[str] = frozenset({"SUPER_ADMIN", "ORG_ADMIN"})


def _filter_metadata(metadata: dict, actor_role: Optional[str]) -> dict:
    """역할에 따라 metadata를 필터링한다.

    - SUPER_ADMIN / ORG_ADMIN: 전체 metadata 반환
    - 그 외 역할 / 익명: `_`로 시작하지 않는 키만 반환 (public_metadata)

    metadata 키 규약:
      - `_`로 시작: 내부/비공개 필드 (예: _internal_id, _system_tag)
      - 그 외: 공개 필드 (public_metadata)
    """
    if actor_role in _ADMIN_ROLES:
        return metadata
    return {k: v for k, v in metadata.items() if not k.startswith("_")}


# ts_headline 옵션 — 키워드 하이라이팅 설정
_HEADLINE_OPTS = "StartSel=<b>, StopSel=</b>, MaxWords=20, MinWords=10, ShortWord=3"
_HEADLINE_OPTS_SHORT = "StartSel=<b>, StopSel=</b>, MaxWords=10, MinWords=5, ShortWord=3"


def _safe_ts_query(q: str) -> str:
    """검색어를 tsquery 문자열로 변환 (특수문자 이스케이프 + prefix 검색 지원)."""
    tokens = q.strip().split()
    if not tokens:
        return ""
    # 각 토큰을 prefix 검색(:*)으로 AND 결합
    parts = []
    for token in tokens:
        # tsquery 특수문자 이스케이프 — 알파벳/숫자/한글만 허용
        cleaned = "".join(c for c in token if c.isalnum())
        if cleaned:
            parts.append(f"{cleaned}:*")
    return " & ".join(parts)


def _get_search_boost_for_type(document_type: Optional[str]) -> dict:
    """Phase 12: SearchPlugin을 통해 타입별 검색 가중치를 조회한다."""
    if not document_type:
        return {}
    try:
        from app.plugins.base import DocumentTypeRegistry
        plugin = DocumentTypeRegistry.instance().get(document_type)
        return plugin.search_plugin().get_boost_config()
    except Exception:
        return {}


class SearchService:
    """검색 서비스 — FTS 기반 문서/노드 검색."""

    # ---------------------------------------------------------------------------
    # 문서 단위 검색
    # ---------------------------------------------------------------------------

    def search_documents(
        self,
        conn: psycopg2.extensions.connection,
        q: str,
        *,
        doc_type: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        sort: str = "relevance",
        page: int = 1,
        limit: int = 20,
        # 권한 컨텍스트: 현재는 role 기반 단순 필터
        actor_role: Optional[str] = None,
    ) -> DocumentSearchResponse:
        ts_query = _safe_ts_query(q)
        if not ts_query:
            return DocumentSearchResponse(
                query=q,
                results=[],
                pagination=SearchPagination(page=page, limit=limit, total=0, has_next=False),
            )

        # 권한 기반 상태 필터: SUPER_ADMIN, ORG_ADMIN은 모든 상태 열람 가능
        visible_statuses = self._resolve_visible_statuses(status, actor_role)

        with conn.cursor() as cur:
            # 총 건수 쿼리
            count_sql, count_params = self._build_document_query(
                ts_query=ts_query,
                doc_type=doc_type,
                visible_statuses=visible_statuses,
                from_date=from_date,
                to_date=to_date,
                sort=sort,
                count_only=True,
            )
            cur.execute(count_sql, count_params)
            total = (cur.fetchone() or {}).get("count", 0)

            # 결과 쿼리
            offset = (page - 1) * limit
            data_sql, data_params = self._build_document_query(
                ts_query=ts_query,
                doc_type=doc_type,
                visible_statuses=visible_statuses,
                from_date=from_date,
                to_date=to_date,
                sort=sort,
                count_only=False,
                limit=limit,
                offset=offset,
            )
            cur.execute(data_sql, data_params)
            rows = cur.fetchall()

        results = [self._map_document_row(row, actor_role) for row in rows]
        return DocumentSearchResponse(
            query=q,
            results=results,
            pagination=SearchPagination(
                page=page,
                limit=limit,
                total=total,
                has_next=(page * limit) < total,
            ),
        )

    def _build_document_query(
        self,
        *,
        ts_query: str,
        doc_type: Optional[str],
        visible_statuses: list[str],
        from_date: Optional[str],
        to_date: Optional[str],
        sort: str,
        count_only: bool,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[str, list]:
        params: list = [ts_query]  # %s[1] = ts_query

        where_clauses = [
            "d.search_vector @@ to_tsquery('simple', %s)"
        ]

        if visible_statuses:
            placeholders = ",".join(["%s"] * len(visible_statuses))
            where_clauses.append(f"d.status IN ({placeholders})")
            params.extend(visible_statuses)

        if doc_type:
            where_clauses.append("d.document_type = %s")
            params.append(doc_type.upper())

        if from_date:
            where_clauses.append("d.created_at >= %s::timestamptz")
            params.append(from_date)

        if to_date:
            where_clauses.append("d.created_at <= %s::timestamptz")
            params.append(to_date)

        where_sql = " AND ".join(where_clauses)

        if count_only:
            sql = f"SELECT COUNT(*) AS count FROM documents d WHERE {where_sql}"
            return sql, params

        # 스니펫 생성
        snippet_params = [ts_query, ts_query]
        select_snippet = (
            f"ts_headline('simple', COALESCE(d.title,''), to_tsquery('simple', %s), '{_HEADLINE_OPTS_SHORT}') AS title_headline,"
            f"ts_headline('simple', COALESCE(d.summary,''), to_tsquery('simple', %s), '{_HEADLINE_OPTS}') AS summary_headline"
        )

        if sort == "relevance":
            rank_expr = "ts_rank(d.search_vector, to_tsquery('simple', %s)) AS rank"
            rank_param = [ts_query]
            order_sql = "ORDER BY rank DESC, d.updated_at DESC"
        elif sort == "created_at":
            rank_expr = "0.0::float AS rank"
            rank_param = []
            order_sql = "ORDER BY d.created_at DESC"
        else:  # updated_at
            rank_expr = "0.0::float AS rank"
            rank_param = []
            order_sql = "ORDER BY d.updated_at DESC"

        all_params = rank_param + snippet_params + params + [limit, offset]

        sql = f"""
            SELECT
                d.id, d.title, d.document_type, d.status, d.summary,
                d.metadata, d.created_by, d.created_at, d.updated_at,
                d.current_published_version_id,
                {rank_expr},
                {select_snippet}
            FROM documents d
            WHERE {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s
        """
        return sql, all_params

    def _map_document_row(self, row: dict, actor_role: Optional[str] = None) -> DocumentSearchResult:
        snippets = []
        title_hl = row.get("title_headline") or ""
        summary_hl = row.get("summary_headline") or ""
        if title_hl and "<b>" in title_hl:
            snippets.append(DocumentSnippet(field="title", text=title_hl))
        if summary_hl and "<b>" in summary_hl:
            snippets.append(DocumentSnippet(field="summary", text=summary_hl))

        return DocumentSearchResult(
            id=str(row["id"]),
            title=row["title"],
            document_type=row["document_type"],
            status=row["status"],
            summary=row.get("summary"),
            metadata=_filter_metadata(row.get("metadata") or {}, actor_role),
            created_by=row.get("created_by"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            current_published_version_id=(
                str(row["current_published_version_id"])
                if row.get("current_published_version_id")
                else None
            ),
            rank=float(row.get("rank") or 0.0),
            snippets=snippets,
        )

    # ---------------------------------------------------------------------------
    # 노드 단위 검색
    # ---------------------------------------------------------------------------

    def search_nodes(
        self,
        conn: psycopg2.extensions.connection,
        q: str,
        *,
        document_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        sort: str = "relevance",
        page: int = 1,
        limit: int = 20,
        actor_role: Optional[str] = None,
    ) -> NodeSearchResponse:
        ts_query = _safe_ts_query(q)
        if not ts_query:
            return NodeSearchResponse(
                query=q,
                results=[],
                pagination=SearchPagination(page=page, limit=limit, total=0, has_next=False),
            )

        visible_statuses = self._resolve_visible_statuses(None, actor_role)

        with conn.cursor() as cur:
            count_sql, count_params = self._build_node_query(
                ts_query=ts_query,
                document_id=document_id,
                doc_type=doc_type,
                visible_statuses=visible_statuses,
                visible_version_statuses=visible_statuses,
                sort=sort,
                count_only=True,
            )
            cur.execute(count_sql, count_params)
            total = (cur.fetchone() or {}).get("count", 0)

            offset = (page - 1) * limit
            data_sql, data_params = self._build_node_query(
                ts_query=ts_query,
                document_id=document_id,
                doc_type=doc_type,
                visible_statuses=visible_statuses,
                visible_version_statuses=visible_statuses,
                sort=sort,
                count_only=False,
                limit=limit,
                offset=offset,
            )
            cur.execute(data_sql, data_params)
            rows = cur.fetchall()

        results = [self._map_node_row(conn, row) for row in rows]
        return NodeSearchResponse(
            query=q,
            results=results,
            pagination=SearchPagination(
                page=page,
                limit=limit,
                total=total,
                has_next=(page * limit) < total,
            ),
        )

    def _build_node_query(
        self,
        *,
        ts_query: str,
        document_id: Optional[str],
        doc_type: Optional[str],
        visible_statuses: list[str],
        visible_version_statuses: list[str],
        sort: str,
        count_only: bool,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[str, list]:
        params: list = [ts_query]

        version_placeholders = ",".join(["%s"] * len(visible_version_statuses))
        where_clauses = [
            "n.search_vector @@ to_tsquery('simple', %s)",
            f"v.status IN ({version_placeholders})",
        ]
        params.extend(visible_version_statuses)

        if visible_statuses:
            placeholders = ",".join(["%s"] * len(visible_statuses))
            where_clauses.append(f"d.status IN ({placeholders})")
            params.extend(visible_statuses)

        if document_id:
            where_clauses.append("d.id = %s::uuid")
            params.append(document_id)

        if doc_type:
            where_clauses.append("d.document_type = %s")
            params.append(doc_type.upper())

        where_sql = " AND ".join(where_clauses)

        if count_only:
            sql = f"""
                SELECT COUNT(*) AS count
                FROM nodes n
                JOIN versions v ON n.version_id = v.id
                JOIN documents d ON v.document_id = d.id
                WHERE {where_sql}
            """
            return sql, params

        snippet_param = [ts_query]
        snippet_sql = f"ts_headline('simple', COALESCE(n.content,''), to_tsquery('simple', %s), '{_HEADLINE_OPTS}') AS content_snippet"

        if sort == "relevance":
            rank_expr = "ts_rank(n.search_vector, to_tsquery('simple', %s)) AS rank"
            rank_param = [ts_query]
            order_sql = "ORDER BY rank DESC"
        else:
            rank_expr = "0.0::float AS rank"
            rank_param = []
            order_sql = "ORDER BY n.order_index ASC"

        all_params = rank_param + snippet_param + params + [limit, offset]

        sql = f"""
            SELECT
                n.id AS node_id,
                n.node_type,
                n.title AS node_title,
                n.order_index,
                n.parent_id,
                v.id AS version_id,
                v.version_number,
                d.id AS document_id,
                d.title AS document_title,
                d.document_type,
                d.status AS document_status,
                {rank_expr},
                {snippet_sql}
            FROM nodes n
            JOIN versions v ON n.version_id = v.id
            JOIN documents d ON v.document_id = d.id
            WHERE {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s
        """
        return sql, all_params

    def _map_node_row(
        self,
        conn: psycopg2.extensions.connection,
        row: dict,
    ) -> NodeSearchResult:
        # breadcrumb: 부모 노드 경로 조회 (최대 3단계)
        breadcrumb = self._get_node_breadcrumb(conn, row.get("parent_id"))

        return NodeSearchResult(
            node_id=str(row["node_id"]),
            node_type=row["node_type"],
            title=row.get("node_title"),
            content_snippet=row.get("content_snippet"),
            order_index=row["order_index"],
            document_id=str(row["document_id"]),
            document_title=row["document_title"],
            document_type=row["document_type"],
            document_status=row["document_status"],
            version_id=str(row["version_id"]),
            version_number=row["version_number"],
            breadcrumb=breadcrumb,
            rank=float(row.get("rank") or 0.0),
        )

    def _get_node_breadcrumb(
        self,
        conn: psycopg2.extensions.connection,
        parent_id: Optional[str],
        max_depth: int = 3,
    ) -> list[NodeBreadcrumb]:
        if not parent_id:
            return []
        breadcrumb = []
        current_id = str(parent_id)
        with conn.cursor() as cur:
            for _ in range(max_depth):
                cur.execute(
                    "SELECT id, title, node_type, parent_id FROM nodes WHERE id = %s::uuid",
                    (current_id,),
                )
                node = cur.fetchone()
                if not node:
                    break
                breadcrumb.insert(
                    0,
                    NodeBreadcrumb(
                        node_id=str(node["id"]),
                        title=node.get("title"),
                        node_type=node["node_type"],
                    ),
                )
                if not node.get("parent_id"):
                    break
                current_id = str(node["parent_id"])
        return breadcrumb

    # ---------------------------------------------------------------------------
    # 하이브리드 검색 (FTS + pgvector RRF)
    # ---------------------------------------------------------------------------

    def search_documents_hybrid(
        self,
        conn: psycopg2.extensions.connection,
        q: str,
        *,
        doc_type: Optional[str] = None,
        status: Optional[str] = None,
        sort: str = "relevance",
        page: int = 1,
        limit: int = 20,
        actor_role: Optional[str] = None,
        rrf_k: int = 60,
        top_k: int = 50,
    ) -> DocumentSearchResponse:
        """FTS + pgvector 유사도 검색 결과를 RRF로 통합한 하이브리드 검색.

        알고리즘:
          1. FTS 상위 top_k 결과 수집 (document 단위)
          2. 벡터 유사도 상위 top_k 결과 수집 (chunk → document 집계)
          3. RRF 스코어 = Σ 1/(rrf_k + rank_i) 로 통합 랭킹
          4. 상위 결과 반환 (페이지네이션)
        """
        visible_statuses = self._resolve_visible_statuses(status, actor_role)

        # --- FTS 결과 수집 ---
        fts_docs: dict[str, float] = {}
        ts_query = _safe_ts_query(q)
        if ts_query:
            fts_sql, fts_params = self._build_document_query(
                ts_query=ts_query,
                doc_type=doc_type,
                visible_statuses=visible_statuses,
                from_date=None,
                to_date=None,
                sort="relevance",
                count_only=False,
                limit=top_k,
                offset=0,
            )
            with conn.cursor() as cur:
                cur.execute(fts_sql, fts_params)
                fts_rows = cur.fetchall()
            for rank_pos, row in enumerate(fts_rows, start=1):
                doc_id = str(row["id"])
                fts_docs[doc_id] = 1.0 / (rrf_k + rank_pos)

        # --- 벡터 검색 결과 수집 (chunk → document 집계) ---
        vec_docs: dict[str, float] = {}
        try:
            from app.services.vectorization_service import vectorization_pipeline
            chunk_results = vectorization_pipeline.semantic_search(
                conn,
                query=q,
                actor_role=actor_role,
                document_type=doc_type,
                top_k=top_k,
            )
            # document별 최고 유사도 청크의 rank 위치로 RRF 계산
            seen_docs: dict[str, int] = {}
            for rank_pos, chunk in enumerate(chunk_results, start=1):
                doc_id = chunk["document_id"]
                if doc_id not in seen_docs:
                    seen_docs[doc_id] = rank_pos
                    vec_docs[doc_id] = 1.0 / (rrf_k + rank_pos)
        except Exception as exc:
            logger.warning("하이브리드 검색 중 벡터 검색 실패 (FTS만 사용): %s", exc)

        # --- RRF 통합 ---
        all_doc_ids = set(fts_docs) | set(vec_docs)
        rrf_scores: dict[str, float] = {}
        for doc_id in all_doc_ids:
            rrf_scores[doc_id] = fts_docs.get(doc_id, 0.0) + vec_docs.get(doc_id, 0.0)

        if not rrf_scores:
            return DocumentSearchResponse(
                query=q,
                results=[],
                pagination=SearchPagination(page=page, limit=limit, total=0, has_next=False),
                search_engine="hybrid_rrf",
            )

        # 정렬된 document_id 목록
        sorted_doc_ids = sorted(rrf_scores, key=lambda d: rrf_scores[d], reverse=True)
        total = len(sorted_doc_ids)

        # 페이지네이션
        offset = (page - 1) * limit
        page_doc_ids = sorted_doc_ids[offset: offset + limit]

        if not page_doc_ids:
            return DocumentSearchResponse(
                query=q,
                results=[],
                pagination=SearchPagination(page=page, limit=limit, total=total, has_next=False),
                search_engine="hybrid_rrf",
            )

        # 문서 상세 정보 조회
        placeholders = ",".join(["%s::uuid"] * len(page_doc_ids))
        with conn.cursor() as cur:
            # 스니펫을 위한 ts_query 빌드
            snippet_ts = ts_query or "''"
            if ts_query:
                cur.execute(
                    f"""
                    SELECT
                        d.id, d.title, d.document_type, d.status, d.summary,
                        d.metadata, d.created_by, d.created_at, d.updated_at,
                        d.current_published_version_id,
                        0.0::float AS rank,
                        ts_headline('simple', COALESCE(d.title,''), to_tsquery('simple', %s), '{_HEADLINE_OPTS_SHORT}') AS title_headline,
                        ts_headline('simple', COALESCE(d.summary,''), to_tsquery('simple', %s), '{_HEADLINE_OPTS}') AS summary_headline
                    FROM documents d
                    WHERE d.id IN ({placeholders})
                      AND d.status IN ({",".join(["%s"] * len(visible_statuses))})
                    """,
                    [ts_query, ts_query] + page_doc_ids + visible_statuses,
                )
            else:
                cur.execute(
                    f"""
                    SELECT
                        d.id, d.title, d.document_type, d.status, d.summary,
                        d.metadata, d.created_by, d.created_at, d.updated_at,
                        d.current_published_version_id,
                        0.0::float AS rank,
                        '' AS title_headline, '' AS summary_headline
                    FROM documents d
                    WHERE d.id IN ({placeholders})
                      AND d.status IN ({",".join(["%s"] * len(visible_statuses))})
                    """,
                    page_doc_ids + visible_statuses,
                )
            rows = cur.fetchall()

        # doc_id → row 매핑
        row_map = {str(r["id"]): r for r in rows}

        results = []
        for doc_id in page_doc_ids:
            row = row_map.get(doc_id)
            if row:
                result = self._map_document_row(row, actor_role)
                result.rank = rrf_scores.get(doc_id, 0.0)
                results.append(result)

        return DocumentSearchResponse(
            query=q,
            results=results,
            pagination=SearchPagination(
                page=page, limit=limit, total=total,
                has_next=(page * limit) < total,
            ),
            search_engine="hybrid_rrf",
        )

    # ---------------------------------------------------------------------------
    # 통합 검색
    # ---------------------------------------------------------------------------

    def search_unified(
        self,
        conn: psycopg2.extensions.connection,
        q: str,
        *,
        doc_type: Optional[str] = None,
        status: Optional[str] = None,
        actor_role: Optional[str] = None,
    ) -> UnifiedSearchResponse:
        doc_result = self.search_documents(
            conn, q, doc_type=doc_type, status=status,
            sort="relevance", page=1, limit=5, actor_role=actor_role
        )
        node_result = self.search_nodes(
            conn, q, doc_type=doc_type,
            sort="relevance", page=1, limit=5, actor_role=actor_role
        )
        return UnifiedSearchResponse(
            query=q,
            documents=doc_result.results,
            nodes=node_result.results,
            total_documents=doc_result.pagination.total,
            total_nodes=node_result.pagination.total,
        )

    # ---------------------------------------------------------------------------
    # 검색 인덱스 현황 (Admin용)
    # ---------------------------------------------------------------------------

    def get_index_stats(
        self,
        conn: psycopg2.extensions.connection,
    ) -> SearchIndexStats:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM search_index_stats ORDER BY table_name")
            rows = cur.fetchall()
        stats = [
            IndexStatsEntry(
                table_name=row["table_name"],
                total_rows=row["total_rows"],
                indexed_rows=row["indexed_rows"],
                unindexed_rows=row["unindexed_rows"],
            )
            for row in rows
        ]
        return SearchIndexStats(stats=stats, retrieved_at=datetime.utcnow())

    # ---------------------------------------------------------------------------
    # 수동 재인덱싱 (Admin용)
    # ---------------------------------------------------------------------------

    def reindex_all(
        self,
        conn: psycopg2.extensions.connection,
    ) -> dict:
        """모든 테이블의 search_vector를 일괄 갱신한다."""
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE documents
                SET search_vector =
                    setweight(to_tsvector('simple', COALESCE(title, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(summary, '')), 'C')
            """)
            doc_count = cur.rowcount

            cur.execute("""
                UPDATE versions
                SET search_vector =
                    setweight(to_tsvector('simple', COALESCE(title_snapshot, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(summary_snapshot, '')), 'B') ||
                    setweight(to_tsvector('simple', COALESCE(change_summary, '')), 'C')
            """)
            ver_count = cur.rowcount

            cur.execute("""
                UPDATE nodes
                SET search_vector =
                    setweight(to_tsvector('simple', COALESCE(title, '')), 'A') ||
                    setweight(to_tsvector('simple', COALESCE(content, '')), 'B')
            """)
            node_count = cur.rowcount

        return {
            "reindexed": {
                "documents": doc_count,
                "versions": ver_count,
                "nodes": node_count,
            }
        }

    # ---------------------------------------------------------------------------
    # 내부 유틸
    # ---------------------------------------------------------------------------

    def _resolve_visible_statuses(
        self,
        requested_status: Optional[str],
        actor_role: Optional[str],
    ) -> list[str]:
        """권한에 따라 열람 가능한 문서 상태 목록 반환.

        - SUPER_ADMIN, ORG_ADMIN: 모든 상태 열람 가능
        - AUTHOR, REVIEWER, APPROVER: published + draft
        - VIEWER 또는 미인증: published 만
        """
        admin_roles = {"SUPER_ADMIN", "ORG_ADMIN"}
        edit_roles = {"AUTHOR", "REVIEWER", "APPROVER", "PUBLISHER"}

        if actor_role in admin_roles:
            all_statuses = ["draft", "published", "archived", "deprecated"]
        elif actor_role in edit_roles:
            all_statuses = ["draft", "published"]
        else:
            all_statuses = ["published"]

        if requested_status and requested_status in all_statuses:
            return [requested_status]
        return all_statuses


    # ---------------------------------------------------------------------------
    # Phase 2 S2: Retriever/Reranker 플러그인 통합 검색
    # ---------------------------------------------------------------------------

    async def search_with_plugins(
        self,
        conn: psycopg2.extensions.connection,
        query: str,
        document_type: str,
        top_k: int = 10,
        filters: Optional[dict] = None,
        retriever_override: Optional[str] = None,
        reranker_override: Optional[str] = None,
    ):
        """Retriever/Reranker 플러그인을 사용한 검색.

        Args:
            conn: DB 연결
            query: 검색 쿼리
            document_type: DocumentType 이름 (빈 문자열이면 전체)
            top_k: 최종 반환 결과 수
            filters: ACL 포함 필터 (actor_role 필수)
            retriever_override: API 파라미터 오버라이드 ("fts"|"vector"|"hybrid")
            reranker_override: API 파라미터 오버라이드 ("cross_encoder"|"rule_based"|"null")

        Returns:
            RetrievalResult 리스트 (Reranker 적용 후 top_k개)
        """
        from app.schemas.retrieval_config import RetrievalConfig
        from app.services.retrieval.retriever_factory import RetrieverFactory
        from app.services.retrieval.reranker_factory import RerankerFactory

        # DocumentType 설정 조회
        config = self._get_retrieval_config(conn, document_type)

        # Retriever/Reranker 결정: API 파라미터 > DocumentType 설정
        retriever_name = retriever_override or config.default_retriever
        reranker_name = reranker_override or config.default_reranker

        retriever = RetrieverFactory.create(
            retriever_name,
            conn,
            config.retriever_params.model_dump(),
        )
        reranker = RerankerFactory.create(
            reranker_name,
            config.reranker_params.model_dump(),
        )

        # Retriever: 후보군 넉넉히 (Reranker 입력용, 최대 100개)
        candidates = await retriever.retrieve(
            query=query,
            document_type=document_type,
            top_k=min(top_k * 5, 100),
            filters=filters,
        )

        # Reranker: 최종 top_k로 압축
        return await reranker.rerank(query, candidates, top_k=top_k)

    def _get_retrieval_config(
        self,
        conn: psycopg2.extensions.connection,
        document_type: str,
    ):
        """document_types 테이블에서 retrieval_config를 조회한다."""
        from app.schemas.retrieval_config import RetrievalConfig

        if not document_type:
            return RetrievalConfig()

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT retrieval_config FROM document_types WHERE type_code = %s",
                    (document_type,),
                )
                row = cur.fetchone()
            if row and row.get("retrieval_config"):
                return RetrievalConfig.model_validate(row["retrieval_config"])
        except Exception as exc:
            logger.warning(
                "Failed to load retrieval_config for document_type=%s: %s — using defaults",
                document_type,
                exc,
            )
        return RetrievalConfig()


search_service = SearchService()
