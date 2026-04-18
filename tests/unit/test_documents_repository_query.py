"""
DocumentsRepository._build_list_query 단위 테스트.

검증 목표:
  - SQL injection 방어: whitelist 외 sort/filter 필드는 무시됨
  - WHERE 절 파라미터 바인딩 순서 정확성
  - ORDER BY 컬럼 whitelist 동작
  - LIMIT / OFFSET 계산
  - 기본값 동작 (빈 query)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")

from app.api.query.models import ParsedListQuery, SortOrder
from app.repositories.documents_repository import _build_list_query


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def build(
    *,
    page: int = 1,
    page_size: int = 20,
    sort_orders: list[SortOrder] | None = None,
    filters: dict | None = None,
) -> tuple[str, list, str, list]:
    q = ParsedListQuery(
        page=page,
        page_size=page_size,
        sort_orders=sort_orders or [],
        filters=filters or {},
    )
    return _build_list_query(q)


# ---------------------------------------------------------------------------
# 기본 동작
# ---------------------------------------------------------------------------

class TestBuildListQueryDefaults:
    def test_returns_four_tuple(self):
        data_sql, data_params, count_sql, count_params = build()
        assert isinstance(data_sql, str)
        assert isinstance(data_params, list)
        assert isinstance(count_sql, str)
        assert isinstance(count_params, list)

    def test_default_order_by(self):
        data_sql, _, _, _ = build()
        assert "ORDER BY created_at DESC" in data_sql

    def test_default_pagination(self):
        _, data_params, _, _ = build(page=1, page_size=20)
        # 마지막 두 파라미터: LIMIT, OFFSET
        assert data_params[-2] == 20  # page_size
        assert data_params[-1] == 0   # offset = (1-1)*20

    def test_page2_offset(self):
        _, data_params, _, _ = build(page=2, page_size=10)
        assert data_params[-2] == 10  # page_size
        assert data_params[-1] == 10  # offset = (2-1)*10

    def test_no_where_clause_by_default(self):
        data_sql, _, count_sql, _ = build()
        assert "WHERE" not in data_sql.upper() or data_sql.upper().count("WHERE") == 0
        assert "WHERE" not in count_sql.upper()

    def test_select_columns_present(self):
        data_sql, _, _, _ = build()
        for col in ("id", "title", "document_type", "status", "created_at", "updated_at"):
            assert col in data_sql

    def test_count_sql_has_count(self):
        _, _, count_sql, _ = build()
        assert "COUNT(*)" in count_sql


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------

class TestBuildListQueryFilters:
    def test_single_filter(self):
        data_sql, data_params, count_sql, count_params = build(
            filters={"status": "PUBLISHED"}
        )
        assert "WHERE" in data_sql
        assert "status = %s" in data_sql
        assert "PUBLISHED" in data_params
        assert "PUBLISHED" in count_params

    def test_multiple_filters(self):
        data_sql, data_params, _, count_params = build(
            filters={"status": "DRAFT", "document_type": "report"}
        )
        assert "status = %s" in data_sql
        assert "document_type = %s" in data_sql
        assert "DRAFT" in data_params
        assert "report" in data_params

    def test_unknown_filter_ignored(self):
        """SQL injection 방어: whitelist 외 필드는 WHERE에 포함되지 않는다."""
        data_sql, data_params, _, _ = build(
            filters={"injected_field": "'; DROP TABLE documents; --"}
        )
        assert "injected_field" not in data_sql
        assert "DROP TABLE" not in data_sql
        # WHERE 절 없어야 함 (알 수 없는 필드만 있었으므로)
        assert "WHERE" not in data_sql.upper()

    def test_owner_id_maps_to_created_by(self):
        """owner_id query param은 created_by 컬럼으로 매핑된다."""
        data_sql, data_params, _, _ = build(filters={"owner_id": "user-123"})
        assert "created_by = %s" in data_sql
        assert "user-123" in data_params

    def test_filter_params_before_pagination(self):
        """파라미터 순서: filter 값들 → LIMIT → OFFSET."""
        _, data_params, _, _ = build(
            filters={"status": "PUBLISHED"},
            page=2,
            page_size=5,
        )
        assert data_params[0] == "PUBLISHED"
        assert data_params[-2] == 5   # LIMIT
        assert data_params[-1] == 5   # OFFSET = (2-1)*5


# ---------------------------------------------------------------------------
# 정렬
# ---------------------------------------------------------------------------

class TestBuildListQuerySort:
    def test_allowed_sort_field(self):
        data_sql, _, _, _ = build(
            sort_orders=[SortOrder(field="title", direction="asc")]
        )
        assert "ORDER BY title ASC" in data_sql

    def test_sort_desc(self):
        data_sql, _, _, _ = build(
            sort_orders=[SortOrder(field="updated_at", direction="desc")]
        )
        assert "ORDER BY updated_at DESC" in data_sql

    def test_multi_sort(self):
        data_sql, _, _, _ = build(
            sort_orders=[
                SortOrder(field="status", direction="asc"),
                SortOrder(field="created_at", direction="desc"),
            ]
        )
        assert "status ASC" in data_sql
        assert "created_at DESC" in data_sql

    def test_unknown_sort_field_ignored(self):
        """SQL injection 방어: whitelist 외 sort 필드는 ORDER BY에 포함되지 않는다."""
        data_sql, _, _, _ = build(
            sort_orders=[SortOrder(field="'; DROP TABLE--", direction="asc")]
        )
        assert "DROP TABLE" not in data_sql
        # 알 수 없는 필드만 있으면 기본 정렬 사용
        assert "created_at DESC" in data_sql

    def test_mixed_known_unknown_sort(self):
        """알 수 없는 필드는 제외되고 알려진 필드만 포함된다."""
        data_sql, _, _, _ = build(
            sort_orders=[
                SortOrder(field="title", direction="asc"),
                SortOrder(field="unknown_field", direction="desc"),
            ]
        )
        assert "title ASC" in data_sql
        assert "unknown_field" not in data_sql


# ---------------------------------------------------------------------------
# 페이지네이션 경계값
# ---------------------------------------------------------------------------

class TestBuildListQueryPagination:
    def test_page_zero_treated_as_page_one(self):
        """page=0은 page=1로 보정된다."""
        _, data_params, _, _ = build(page=0, page_size=10)
        assert data_params[-1] == 0  # offset = 0

    def test_page_size_zero_treated_as_default(self):
        """page_size=0은 기본값 20으로 보정된다."""
        _, data_params, _, _ = build(page=1, page_size=0)
        assert data_params[-2] == 20

    def test_large_page(self):
        _, data_params, _, _ = build(page=100, page_size=50)
        assert data_params[-2] == 50
        assert data_params[-1] == (100 - 1) * 50


# ---------------------------------------------------------------------------
# count_sql 독립성
# ---------------------------------------------------------------------------

class TestBuildListQueryCountSql:
    def test_count_sql_has_no_limit(self):
        _, _, count_sql, count_params = build(page=2, page_size=5)
        assert "LIMIT" not in count_sql.upper()
        assert "OFFSET" not in count_sql.upper()
        # count_params에는 pagination 파라미터 없어야 함
        assert 5 not in count_params
        assert 5 not in count_params

    def test_count_params_match_filter_only(self):
        _, data_params, _, count_params = build(
            filters={"status": "PUBLISHED"},
            page=3,
            page_size=10,
        )
        # count_params = filter 파라미터만
        assert count_params == ["PUBLISHED"]
        # data_params = filter + LIMIT + OFFSET
        assert data_params == ["PUBLISHED", 10, 20]
