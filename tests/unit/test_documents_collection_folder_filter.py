"""
S3 Phase 2 FG 2-1 — documents?collection=&folder=&include_subfolders= 필터 검증.

_build_list_query 가 컬렉션/폴더 필터를 WHERE 절에 올바르게 접붙이는지 확인.
'뷰 ≠ 권한' 규약에 따라 viewer Scope 필터는 별도로 AND 결합.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


COLL = "cccc-0000-0000-0000-000000000001"
FOLDER = "ffff-0000-0000-0000-000000000001"
SCOPE = "ssss-0000-0000-0000-000000000001"


def Q(**filters):
    return SimpleNamespace(
        filters=filters, sort_orders=[], page=1, page_size=20,
    )


class TestCollectionFilter:
    def test_collection_param_generates_subquery(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, data_params, _, _ = _build_list_query(Q(collection=COLL))
        assert "collection_documents" in data_sql
        assert "collection_id = %s" in data_sql
        assert COLL in data_params

    def test_collection_combined_with_scope(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, data_params, _, _ = _build_list_query(
            Q(collection=COLL), viewer_scope_profile_ids=[SCOPE],
        )
        assert "collection_documents" in data_sql
        assert "scope_profile_id IN" in data_sql
        assert COLL in data_params
        assert SCOPE in data_params


class TestFolderFilter:
    def test_folder_param_generates_subquery(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, data_params, _, _ = _build_list_query(Q(folder=FOLDER))
        assert "document_folder" in data_sql
        assert "folder_id = %s" in data_sql
        assert FOLDER in data_params

    def test_folder_with_include_subfolders(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, data_params, _, _ = _build_list_query(
            Q(folder=FOLDER, include_subfolders="true"),
        )
        # path LIKE prefix 매칭 서브쿼리가 들어가야 함
        assert "path LIKE" in data_sql
        assert "folders" in data_sql
        assert FOLDER in data_params

    def test_folder_without_include_subfolders_is_literal(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, _, _, _ = _build_list_query(
            Q(folder=FOLDER, include_subfolders="false"),
        )
        assert "path LIKE" not in data_sql  # 하위 포함 아님 → prefix 매칭 없음
        assert "folder_id = %s" in data_sql


class TestCombinedFilters:
    def test_collection_and_folder_together(self):
        from app.repositories.documents_repository import _build_list_query
        data_sql, data_params, _, _ = _build_list_query(
            Q(collection=COLL, folder=FOLDER),
        )
        assert "collection_documents" in data_sql
        assert "document_folder" in data_sql
        # AND 로 결합
        assert data_sql.count("AND") >= 1

    def test_special_keys_not_treated_as_column(self):
        """`collection`, `folder`, `include_subfolders` 가 col=%s 일반 필터로 잘못 추가되지 않아야 함."""
        from app.repositories.documents_repository import _build_list_query
        data_sql, _, _, _ = _build_list_query(Q(collection=COLL))
        # 일반 필터 매핑 (예: 'collection = %s') 형태로 실리면 안 됨
        assert "collection = %s" not in data_sql
        assert "folder = %s" not in data_sql
        assert "include_subfolders = %s" not in data_sql
