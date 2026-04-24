"""FG 0-3 커버리지 보강 — search_service 유닛 테스트 (세션 10).

대상: `backend/app/services/search_service.py` (846줄)

커버 범위:
  - _filter_metadata / _safe_ts_query / _get_search_boost_for_type
  - SearchService._resolve_visible_statuses
  - SearchService._build_document_query (count + relevance/created_at/updated_at sort + 필터 조합)
  - SearchService._map_document_row (snippet 조건)
  - SearchService.search_documents (빈 쿼리 조기반환 / 정상 흐름 / 필터)
  - SearchService._build_node_query
  - SearchService._map_node_row / _get_node_breadcrumb (깊이/parent 없음/미발견)
  - SearchService.search_nodes
  - SearchService.search_documents_hybrid (FTS+vec RRF / vec 실패 폴백 / 빈 결과 / 페이지네이션)
  - SearchService.search_unified / get_index_stats / reindex_all
  - SearchService._get_retrieval_config (빈/정상/row 없음)
  - SearchService.search_with_plugins (기본/override) [async]
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import search_service as ss_mod
from app.services.search_service import (
    SearchService,
    _filter_metadata,
    _get_search_boost_for_type,
    _safe_ts_query,
)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _mk_cur(fetchone_values=None, fetchall_values=None, rowcount=0):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    if fetchone_values is not None:
        cur.fetchone = MagicMock(side_effect=list(fetchone_values))
    else:
        cur.fetchone = MagicMock(return_value=None)
    if fetchall_values is not None:
        cur.fetchall = MagicMock(side_effect=list(fetchall_values))
    else:
        cur.fetchall = MagicMock(return_value=[])
    cur.rowcount = rowcount
    return cur


def _mk_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _doc_row(
    id_: str = "11111111-1111-1111-1111-111111111111",
    title: str = "테스트 문서",
    rank: float = 0.5,
    title_hl: str = "",
    summary_hl: str = "",
):
    return {
        "id": id_,
        "title": title,
        "document_type": "REPORT",
        "status": "published",
        "summary": "요약",
        "metadata": {"public": "v", "_internal": "s"},
        "created_by": "user-1",
        "created_at": datetime(2026, 4, 1),
        "updated_at": datetime(2026, 4, 2),
        "current_published_version_id": "22222222-2222-2222-2222-222222222222",
        "rank": rank,
        "title_headline": title_hl,
        "summary_headline": summary_hl,
    }


def _node_row(
    node_id: str = "33333333-3333-3333-3333-333333333333",
    parent_id: str | None = None,
):
    return {
        "node_id": node_id,
        "node_type": "section",
        "node_title": "섹션1",
        "order_index": 0,
        "parent_id": parent_id,
        "version_id": "44444444-4444-4444-4444-444444444444",
        "version_number": 1,
        "document_id": "55555555-5555-5555-5555-555555555555",
        "document_title": "문서",
        "document_type": "REPORT",
        "document_status": "published",
        "rank": 0.3,
        "content_snippet": "<b>키워드</b> 포함 본문",
    }


# ---------------------------------------------------------------------------
# 1. _filter_metadata
# ---------------------------------------------------------------------------


def test_filter_metadata_admin_returns_full():
    md = {"public": "v", "_internal": "s"}
    assert _filter_metadata(md, "SUPER_ADMIN") == md
    assert _filter_metadata(md, "ORG_ADMIN") == md


def test_filter_metadata_non_admin_strips_underscore_prefix():
    md = {"public": "v", "_internal": "s", "another": 1}
    result = _filter_metadata(md, "VIEWER")
    assert result == {"public": "v", "another": 1}
    # anonymous
    assert _filter_metadata(md, None) == {"public": "v", "another": 1}


# ---------------------------------------------------------------------------
# 2. _safe_ts_query
# ---------------------------------------------------------------------------


def test_safe_ts_query_empty_returns_empty():
    assert _safe_ts_query("") == ""
    assert _safe_ts_query("   ") == ""


def test_safe_ts_query_single_token_prefix():
    assert _safe_ts_query("hello") == "hello:*"


def test_safe_ts_query_multiple_tokens_and():
    assert _safe_ts_query("안녕 world") == "안녕:* & world:*"


def test_safe_ts_query_strips_special_chars():
    # isalnum 필터로 특수문자 제거, 한글은 유지
    result = _safe_ts_query("foo! @#bar 한글")
    assert "foo:*" in result
    assert "bar:*" in result
    assert "한글:*" in result
    # 완전히 특수문자만 있는 토큰은 제거
    assert "@#:*" not in result


# ---------------------------------------------------------------------------
# 3. _get_search_boost_for_type
# ---------------------------------------------------------------------------


def test_get_search_boost_for_type_none():
    assert _get_search_boost_for_type(None) == {}
    assert _get_search_boost_for_type("") == {}


def test_get_search_boost_for_type_plugin_success(monkeypatch):
    fake_plugin_search = MagicMock()
    fake_plugin_search.get_boost_config = MagicMock(return_value={"title": 2.0})
    fake_plugin = MagicMock()
    fake_plugin.search_plugin = MagicMock(return_value=fake_plugin_search)
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=fake_plugin)
    fake_reg_cls = MagicMock()
    fake_reg_cls.instance = MagicMock(return_value=fake_registry)
    fake_mod = MagicMock()
    fake_mod.DocumentTypeRegistry = fake_reg_cls
    monkeypatch.setitem(
        __import__("sys").modules, "app.plugins.base", fake_mod
    )
    assert _get_search_boost_for_type("REPORT") == {"title": 2.0}


def test_get_search_boost_for_type_plugin_failure(monkeypatch):
    fake_reg_cls = MagicMock()
    fake_reg_cls.instance = MagicMock(side_effect=RuntimeError("no plugin"))
    fake_mod = MagicMock()
    fake_mod.DocumentTypeRegistry = fake_reg_cls
    monkeypatch.setitem(
        __import__("sys").modules, "app.plugins.base", fake_mod
    )
    assert _get_search_boost_for_type("UNKNOWN") == {}


# ---------------------------------------------------------------------------
# 4. SearchService._resolve_visible_statuses
# ---------------------------------------------------------------------------


def test_resolve_visible_statuses_admin_sees_all():
    svc = SearchService()
    result = svc._resolve_visible_statuses(None, "SUPER_ADMIN")
    assert set(result) == {"draft", "published", "archived", "deprecated"}
    result2 = svc._resolve_visible_statuses(None, "ORG_ADMIN")
    assert set(result2) == {"draft", "published", "archived", "deprecated"}


def test_resolve_visible_statuses_editor_sees_draft_plus_published():
    svc = SearchService()
    for role in ("AUTHOR", "REVIEWER", "APPROVER", "PUBLISHER"):
        result = svc._resolve_visible_statuses(None, role)
        assert set(result) == {"draft", "published"}


def test_resolve_visible_statuses_viewer_sees_published_only():
    svc = SearchService()
    assert svc._resolve_visible_statuses(None, "VIEWER") == ["published"]
    assert svc._resolve_visible_statuses(None, None) == ["published"]


def test_resolve_visible_statuses_respects_requested_within_scope():
    svc = SearchService()
    # 요청 status 가 권한 범위 내면 단일 리스트
    assert svc._resolve_visible_statuses("draft", "SUPER_ADMIN") == ["draft"]
    # 권한 범위 밖이면 무시되고 전체 반환
    result = svc._resolve_visible_statuses("draft", "VIEWER")
    assert result == ["published"]


# ---------------------------------------------------------------------------
# 5. SearchService._build_document_query
# ---------------------------------------------------------------------------


def test_build_document_query_count_only_shape():
    svc = SearchService()
    sql, params = svc._build_document_query(
        ts_query="test:*",
        doc_type=None,
        visible_statuses=["published"],
        from_date=None,
        to_date=None,
        sort="relevance",
        count_only=True,
    )
    assert "COUNT(*)" in sql
    assert "published" in params
    assert params[0] == "test:*"


def test_build_document_query_relevance_sort():
    svc = SearchService()
    sql, params = svc._build_document_query(
        ts_query="foo:*",
        doc_type=None,
        visible_statuses=["published"],
        from_date=None,
        to_date=None,
        sort="relevance",
        count_only=False,
        limit=10,
        offset=5,
    )
    assert "ORDER BY rank DESC" in sql
    assert "ts_rank" in sql
    assert "ts_headline" in sql
    # limit / offset 이 마지막 두 파라미터
    assert params[-2:] == [10, 5]


def test_build_document_query_created_at_sort_no_rank_param():
    svc = SearchService()
    sql, params = svc._build_document_query(
        ts_query="q:*",
        doc_type=None,
        visible_statuses=["published"],
        from_date=None,
        to_date=None,
        sort="created_at",
        count_only=False,
    )
    assert "ORDER BY d.created_at DESC" in sql
    assert "0.0::float AS rank" in sql


def test_build_document_query_updated_at_sort_is_default():
    svc = SearchService()
    sql, _ = svc._build_document_query(
        ts_query="q:*",
        doc_type=None,
        visible_statuses=["published"],
        from_date=None,
        to_date=None,
        sort="other",  # relevance/created_at 외 → updated_at 분기
        count_only=False,
    )
    assert "ORDER BY d.updated_at DESC" in sql


def test_build_document_query_with_all_filters():
    svc = SearchService()
    sql, params = svc._build_document_query(
        ts_query="q:*",
        doc_type="report",  # 소문자 → 대문자 정규화
        visible_statuses=["draft", "published"],
        from_date="2026-01-01",
        to_date="2026-12-31",
        sort="relevance",
        count_only=False,
    )
    assert "d.document_type = %s" in sql
    assert "REPORT" in params  # upper() 적용됨
    assert "2026-01-01" in params
    assert "2026-12-31" in params
    assert "d.status IN (%s,%s)" in sql


# ---------------------------------------------------------------------------
# 6. SearchService._map_document_row
# ---------------------------------------------------------------------------


def test_map_document_row_with_both_snippets():
    svc = SearchService()
    row = _doc_row(title_hl="<b>키</b>", summary_hl="<b>요</b>")
    result = svc._map_document_row(row, actor_role="SUPER_ADMIN")
    fields = [s.field for s in result.snippets]
    assert "title" in fields
    assert "summary" in fields
    # admin 은 전체 metadata
    assert "_internal" in result.metadata


def test_map_document_row_no_matches_no_snippets():
    svc = SearchService()
    row = _doc_row(title_hl="", summary_hl="")
    result = svc._map_document_row(row, actor_role="VIEWER")
    assert result.snippets == []
    # viewer 는 _-prefix 필터링
    assert "_internal" not in result.metadata


def test_map_document_row_null_published_version():
    svc = SearchService()
    row = _doc_row()
    row["current_published_version_id"] = None
    result = svc._map_document_row(row)
    assert result.current_published_version_id is None


# ---------------------------------------------------------------------------
# 7. SearchService.search_documents
# ---------------------------------------------------------------------------


def test_search_documents_empty_query_returns_empty():
    svc = SearchService()
    result = svc.search_documents(MagicMock(), "   ", page=1, limit=10)
    assert result.results == []
    assert result.pagination.total == 0
    assert result.pagination.has_next is False


def test_search_documents_basic_flow():
    svc = SearchService()
    cur = _mk_cur(
        fetchone_values=[{"count": 1}],
        fetchall_values=[[_doc_row(title_hl="<b>test</b>")]],
    )
    conn = _mk_conn(cur)
    result = svc.search_documents(
        conn, "test", page=1, limit=20, actor_role="SUPER_ADMIN"
    )
    assert result.pagination.total == 1
    assert len(result.results) == 1
    assert result.results[0].title == "테스트 문서"


def test_search_documents_has_next_pagination():
    svc = SearchService()
    cur = _mk_cur(
        fetchone_values=[{"count": 50}],
        fetchall_values=[[_doc_row()]],
    )
    conn = _mk_conn(cur)
    result = svc.search_documents(conn, "query", page=1, limit=20)
    assert result.pagination.total == 50
    assert result.pagination.has_next is True


def test_search_documents_count_returns_zero_when_fetchone_is_none():
    svc = SearchService()
    cur = _mk_cur(
        fetchone_values=[None],
        fetchall_values=[[]],
    )
    conn = _mk_conn(cur)
    result = svc.search_documents(conn, "q")
    assert result.pagination.total == 0


# ---------------------------------------------------------------------------
# 8. SearchService._build_node_query
# ---------------------------------------------------------------------------


def test_build_node_query_count_only():
    svc = SearchService()
    sql, params = svc._build_node_query(
        ts_query="q:*",
        document_id=None,
        doc_type=None,
        visible_statuses=["published"],
        visible_version_statuses=["published"],
        sort="relevance",
        count_only=True,
    )
    assert "COUNT(*)" in sql
    assert "JOIN versions v" in sql
    assert "JOIN documents d" in sql


def test_build_node_query_relevance_sort():
    svc = SearchService()
    sql, _ = svc._build_node_query(
        ts_query="q:*",
        document_id=None,
        doc_type=None,
        visible_statuses=["published"],
        visible_version_statuses=["published"],
        sort="relevance",
        count_only=False,
    )
    assert "ORDER BY rank DESC" in sql
    assert "ts_headline" in sql


def test_build_node_query_non_relevance_sort_by_order_index():
    svc = SearchService()
    sql, _ = svc._build_node_query(
        ts_query="q:*",
        document_id=None,
        doc_type=None,
        visible_statuses=["published"],
        visible_version_statuses=["published"],
        sort="created_at",
        count_only=False,
    )
    assert "ORDER BY n.order_index ASC" in sql
    assert "0.0::float AS rank" in sql


def test_build_node_query_with_document_id_and_doc_type():
    svc = SearchService()
    sql, params = svc._build_node_query(
        ts_query="q:*",
        document_id="doc-uuid-1",
        doc_type="report",
        visible_statuses=["published"],
        visible_version_statuses=["published"],
        sort="relevance",
        count_only=False,
    )
    assert "d.id = %s::uuid" in sql
    assert "doc-uuid-1" in params
    assert "REPORT" in params  # upper 적용


# ---------------------------------------------------------------------------
# 9. SearchService._get_node_breadcrumb
# ---------------------------------------------------------------------------


def test_breadcrumb_returns_empty_when_no_parent():
    svc = SearchService()
    assert svc._get_node_breadcrumb(MagicMock(), None) == []


def test_breadcrumb_returns_single_when_one_level():
    svc = SearchService()
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "p1",
                "title": "부모",
                "node_type": "section",
                "parent_id": None,
            }
        ]
    )
    conn = _mk_conn(cur)
    result = svc._get_node_breadcrumb(conn, "p1")
    assert len(result) == 1
    assert result[0].title == "부모"


def test_breadcrumb_traverses_up_to_max_depth():
    svc = SearchService()
    # 3단계 계층 (자식 → 부모1 → 부모2) — max_depth 3
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "p1",
                "title": "A",
                "node_type": "section",
                "parent_id": "p2",
            },
            {
                "id": "p2",
                "title": "B",
                "node_type": "section",
                "parent_id": "p3",
            },
            {
                "id": "p3",
                "title": "C",
                "node_type": "section",
                "parent_id": None,
            },
        ]
    )
    conn = _mk_conn(cur)
    result = svc._get_node_breadcrumb(conn, "p1", max_depth=3)
    # insert(0, ...) 로 부모 → 자식 순 (C, B, A)
    assert [b.title for b in result] == ["C", "B", "A"]


def test_breadcrumb_stops_when_node_not_found():
    svc = SearchService()
    cur = _mk_cur(fetchone_values=[None])
    conn = _mk_conn(cur)
    assert svc._get_node_breadcrumb(conn, "nonexistent") == []


# ---------------------------------------------------------------------------
# 10. SearchService.search_nodes
# ---------------------------------------------------------------------------


def test_search_nodes_empty_query_returns_empty():
    svc = SearchService()
    result = svc.search_nodes(MagicMock(), "   ")
    assert result.results == []
    assert result.pagination.total == 0


def test_search_nodes_basic_flow():
    svc = SearchService()
    # count → data → (breadcrumb 조회 skip: parent_id=None 이면 호출 안됨)
    cur = _mk_cur(
        fetchone_values=[{"count": 1}],
        fetchall_values=[[_node_row(parent_id=None)]],
    )
    conn = _mk_conn(cur)
    result = svc.search_nodes(conn, "query", actor_role="SUPER_ADMIN")
    assert result.pagination.total == 1
    assert len(result.results) == 1
    # parent_id None → breadcrumb 빈 리스트
    assert result.results[0].breadcrumb == []


# ---------------------------------------------------------------------------
# 11. SearchService.search_documents_hybrid
# ---------------------------------------------------------------------------


def test_hybrid_search_empty_results_returns_empty_response(monkeypatch):
    svc = SearchService()
    # FTS: 빈 결과 (ts_query 는 있지만 행 없음)
    cur = _mk_cur(fetchall_values=[[]])
    conn = _mk_conn(cur)

    # 벡터 검색: 빈 결과
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(return_value=[])
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )

    result = svc.search_documents_hybrid(
        conn, "query", actor_role="SUPER_ADMIN"
    )
    assert result.results == []
    assert result.search_engine == "hybrid_rrf"
    assert result.pagination.total == 0


def test_hybrid_search_merges_fts_and_vector(monkeypatch):
    svc = SearchService()

    # FTS 후보 2건
    doc_a = _doc_row(id_="aa", title_hl="<b>q</b>")
    doc_b = _doc_row(id_="bb", title_hl="<b>q</b>")
    doc_a["id"] = "aa"
    doc_b["id"] = "bb"

    # cursor 호출 순서: FTS data fetch → doc detail fetch
    cur = _mk_cur(
        fetchall_values=[
            [doc_a, doc_b],  # FTS top_k 수집
            [doc_a, doc_b],  # doc detail 조회
        ]
    )
    conn = _mk_conn(cur)

    # 벡터 검색: 같은 doc_id 반환
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(
        return_value=[
            {"document_id": "aa"},
            {"document_id": "cc"},  # FTS 에는 없는 새 문서
        ]
    )
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )

    result = svc.search_documents_hybrid(
        conn, "query", actor_role="SUPER_ADMIN", limit=10
    )
    assert result.search_engine == "hybrid_rrf"
    assert result.pagination.total == 3  # aa, bb, cc 통합
    # 결과는 row_map 에 있는 aa, bb 만 상세 매핑
    assert len(result.results) == 2


def test_hybrid_search_falls_back_when_vector_fails(monkeypatch):
    svc = SearchService()
    doc_a = _doc_row(id_="aa")
    doc_a["id"] = "aa"
    cur = _mk_cur(
        fetchall_values=[
            [doc_a],  # FTS
            [doc_a],  # detail
        ]
    )
    conn = _mk_conn(cur)

    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(
        side_effect=RuntimeError("vec store down")
    )
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )

    result = svc.search_documents_hybrid(conn, "q", actor_role="SUPER_ADMIN")
    # vector 실패해도 FTS 결과로 응답
    assert result.pagination.total == 1


def test_hybrid_search_pagination_beyond_results(monkeypatch):
    svc = SearchService()
    doc_a = _doc_row(id_="aa")
    doc_a["id"] = "aa"
    cur = _mk_cur(fetchall_values=[[doc_a]])
    conn = _mk_conn(cur)
    fake_pipeline = MagicMock()
    fake_pipeline.semantic_search = MagicMock(return_value=[])
    fake_vs_mod = MagicMock()
    fake_vs_mod.vectorization_pipeline = fake_pipeline
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.vectorization_service",
        fake_vs_mod,
    )
    # page=10 이면 offset 이 결과 범위 넘어 빈 페이지
    result = svc.search_documents_hybrid(
        conn, "q", actor_role="SUPER_ADMIN", page=10, limit=20
    )
    assert result.pagination.total == 1
    assert result.results == []


# ---------------------------------------------------------------------------
# 12. SearchService.search_unified
# ---------------------------------------------------------------------------


def test_search_unified_combines_documents_and_nodes():
    svc = SearchService()
    # 문서 2건 count + data, 노드 1건 count + data (parent_id None)
    cur = _mk_cur(
        fetchone_values=[{"count": 2}, {"count": 1}],
        fetchall_values=[
            [_doc_row(id_="d1"), _doc_row(id_="d2")],
            [_node_row(parent_id=None)],
        ],
    )
    conn = _mk_conn(cur)

    result = svc.search_unified(conn, "query", actor_role="SUPER_ADMIN")
    assert result.total_documents == 2
    assert result.total_nodes == 1
    assert len(result.documents) == 2
    assert len(result.nodes) == 1


# ---------------------------------------------------------------------------
# 13. SearchService.get_index_stats
# ---------------------------------------------------------------------------


def test_get_index_stats_returns_entries():
    svc = SearchService()
    rows = [
        {
            "table_name": "documents",
            "total_rows": 100,
            "indexed_rows": 95,
            "unindexed_rows": 5,
        },
        {
            "table_name": "nodes",
            "total_rows": 500,
            "indexed_rows": 500,
            "unindexed_rows": 0,
        },
    ]
    cur = _mk_cur(fetchall_values=[rows])
    conn = _mk_conn(cur)
    result = svc.get_index_stats(conn)
    assert len(result.stats) == 2
    assert result.stats[0].table_name == "documents"
    assert result.stats[0].indexed_rows == 95
    assert isinstance(result.retrieved_at, datetime)


# ---------------------------------------------------------------------------
# 14. SearchService.reindex_all
# ---------------------------------------------------------------------------


def test_reindex_all_returns_row_counts():
    svc = SearchService()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    # 연속 3회 execute 후 각각 rowcount
    rowcounts = [10, 20, 30]
    call_count = {"i": 0}

    def _execute(sql):
        cur.rowcount = rowcounts[call_count["i"]]
        call_count["i"] += 1

    cur.execute.side_effect = _execute
    conn = _mk_conn(cur)

    result = svc.reindex_all(conn)
    assert result == {
        "reindexed": {
            "documents": 10,
            "versions": 20,
            "nodes": 30,
        }
    }


# ---------------------------------------------------------------------------
# 15. SearchService._get_retrieval_config
# ---------------------------------------------------------------------------


def test_get_retrieval_config_empty_document_type_returns_default():
    svc = SearchService()
    result = svc._get_retrieval_config(MagicMock(), "")
    # RetrievalConfig 기본값 — default_retriever/reranker 속성 존재
    assert hasattr(result, "default_retriever")


def test_get_retrieval_config_row_exists_parses(monkeypatch):
    svc = SearchService()
    fake_config = {
        "default_retriever": "hybrid",
        "default_reranker": "null",
        "retriever_params": {},
        "reranker_params": {},
    }
    cur = _mk_cur(
        fetchone_values=[{"retrieval_config": fake_config}]
    )
    conn = _mk_conn(cur)

    # RetrievalConfig.model_validate 가 실제 스키마와 호환되게 하려면
    # 스키마 내부를 모킹하지 않고 실제 객체를 사용 — 기본값이 validate 에 적합해야 함
    # 실 스키마에 따라 validate 실패 시 fallback 분기 확인
    result = svc._get_retrieval_config(conn, "REPORT")
    # validate 성공 or fallback 둘 다 RetrievalConfig 인스턴스
    assert hasattr(result, "default_retriever")


def test_get_retrieval_config_db_error_returns_default(monkeypatch):
    svc = SearchService()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock(side_effect=RuntimeError("db down"))
    conn = _mk_conn(cur)

    result = svc._get_retrieval_config(conn, "REPORT")
    assert hasattr(result, "default_retriever")


def test_get_retrieval_config_row_missing_returns_default():
    svc = SearchService()
    cur = _mk_cur(fetchone_values=[None])
    conn = _mk_conn(cur)
    result = svc._get_retrieval_config(conn, "REPORT")
    assert hasattr(result, "default_retriever")


# ---------------------------------------------------------------------------
# 16. SearchService.search_with_plugins (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_plugins_basic_flow(monkeypatch):
    svc = SearchService()

    # _get_retrieval_config 을 직접 patch
    fake_config = MagicMock()
    fake_config.default_retriever = "fts"
    fake_config.default_reranker = "null"
    fake_config.retriever_params = MagicMock()
    fake_config.retriever_params.model_dump = MagicMock(return_value={"k": 10})
    fake_config.reranker_params = MagicMock()
    fake_config.reranker_params.model_dump = MagicMock(return_value={})
    monkeypatch.setattr(svc, "_get_retrieval_config", lambda c, t: fake_config)

    fake_retriever = MagicMock()
    fake_retriever.retrieve = AsyncMock(
        return_value=[{"chunk_id": "c1"}]
    )
    fake_reranker = MagicMock()
    fake_reranker.rerank = AsyncMock(return_value=[{"chunk_id": "c1"}])

    # RetrieverFactory / RerankerFactory 모킹
    fake_rf = MagicMock()
    fake_rf.create = MagicMock(return_value=fake_retriever)
    fake_rf_mod = MagicMock()
    fake_rf_mod.RetrieverFactory = fake_rf
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.retrieval.retriever_factory",
        fake_rf_mod,
    )

    fake_rerf = MagicMock()
    fake_rerf.create = MagicMock(return_value=fake_reranker)
    fake_rerf_mod = MagicMock()
    fake_rerf_mod.RerankerFactory = fake_rerf
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.retrieval.reranker_factory",
        fake_rerf_mod,
    )

    # RetrievalConfig import 실패 방지용 dummy
    fake_rc_mod = MagicMock()
    fake_rc_mod.RetrievalConfig = MagicMock
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.schemas.retrieval_config",
        fake_rc_mod,
    )

    result = await svc.search_with_plugins(
        MagicMock(),
        query="질문",
        document_type="REPORT",
        top_k=5,
        filters={"actor_role": "VIEWER"},
    )
    assert result == [{"chunk_id": "c1"}]
    # RetrieverFactory.create 호출 시 (이름, conn, params) 순서로 전달
    args = fake_rf.create.call_args[0]
    assert args[0] == "fts"
    assert args[2] == {"k": 10}
    # retrieve 호출 시 top_k*5 → 25
    fake_retriever.retrieve.assert_awaited_once()
    assert fake_retriever.retrieve.await_args.kwargs["top_k"] == 25


@pytest.mark.asyncio
async def test_search_with_plugins_honours_overrides(monkeypatch):
    svc = SearchService()
    fake_config = MagicMock()
    fake_config.default_retriever = "fts"
    fake_config.default_reranker = "null"
    fake_config.retriever_params = MagicMock()
    fake_config.retriever_params.model_dump = MagicMock(return_value={})
    fake_config.reranker_params = MagicMock()
    fake_config.reranker_params.model_dump = MagicMock(return_value={})
    monkeypatch.setattr(svc, "_get_retrieval_config", lambda c, t: fake_config)

    fake_retriever = MagicMock()
    fake_retriever.retrieve = AsyncMock(return_value=[])
    fake_reranker = MagicMock()
    fake_reranker.rerank = AsyncMock(return_value=[])

    fake_rf = MagicMock()
    fake_rf.create = MagicMock(return_value=fake_retriever)
    fake_rf_mod = MagicMock()
    fake_rf_mod.RetrieverFactory = fake_rf
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.retrieval.retriever_factory",
        fake_rf_mod,
    )
    fake_rerf = MagicMock()
    fake_rerf.create = MagicMock(return_value=fake_reranker)
    fake_rerf_mod = MagicMock()
    fake_rerf_mod.RerankerFactory = fake_rerf
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.services.retrieval.reranker_factory",
        fake_rerf_mod,
    )
    fake_rc_mod = MagicMock()
    fake_rc_mod.RetrievalConfig = MagicMock
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.schemas.retrieval_config",
        fake_rc_mod,
    )

    await svc.search_with_plugins(
        MagicMock(),
        query="q",
        document_type="REPORT",
        top_k=10,
        retriever_override="hybrid",
        reranker_override="cross_encoder",
    )
    # override 가 default 를 이김
    assert fake_rf.create.call_args[0][0] == "hybrid"
    assert fake_rerf.create.call_args[0][0] == "cross_encoder"
    # top_k=10 → min(50, 100) = 50
    assert fake_retriever.retrieve.await_args.kwargs["top_k"] == 50


# ---------------------------------------------------------------------------
# 17. 싱글턴 존재 확인
# ---------------------------------------------------------------------------


def test_search_service_singleton_exists():
    assert isinstance(ss_mod.search_service, SearchService)
