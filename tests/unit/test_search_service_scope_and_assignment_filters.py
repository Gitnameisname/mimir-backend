"""
S3 Phase 2 FG 2-1 UX 5차 회귀 — search_service 의 ACL / 컬렉션·폴더 필터 wiring.

_build_document_query 가 신규 파라미터를 받아 안전한 SQL 을 구성하는지 검증.
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


pytestmark = pytest.mark.unit


def _build(**kw):
    from app.services.search_service import search_service
    base = dict(
        ts_query="foo",
        doc_type=None,
        visible_statuses=[],
        from_date=None,
        to_date=None,
        sort="relevance",
        count_only=True,
    )
    base.update(kw)
    return search_service._build_document_query(**base)


class TestScopeFilter:
    def test_none_skips(self):
        sql, params = _build(viewer_scope_profile_ids=None)
        assert "d.scope_profile_id IN" not in sql
        assert "1 = 0" not in sql

    def test_empty_blocks(self):
        sql, _ = _build(viewer_scope_profile_ids=[])
        assert "1 = 0" in sql

    def test_ids_produce_in_clause(self):
        sql, params = _build(viewer_scope_profile_ids=["s1", "s2"])
        assert "d.scope_profile_id IN (%s, %s)" in sql
        assert "s1" in params and "s2" in params


class TestCollectionFolderFilters:
    def test_collection_only(self):
        sql, params = _build(collection_id="c1")
        assert "collection_documents" in sql
        assert "c1" in params

    def test_folder_leaf(self):
        sql, params = _build(folder_id="f1")
        assert "document_folder" in sql
        assert "path LIKE" not in sql
        assert "f1" in params

    def test_folder_with_subfolders(self):
        sql, params = _build(folder_id="f1", include_subfolders=True)
        assert "path LIKE" in sql
        assert "f1" in params

    def test_collection_and_folder_together(self):
        sql, params = _build(collection_id="c1", folder_id="f1")
        assert "collection_documents" in sql and "document_folder" in sql
        assert "c1" in params and "f1" in params


class TestFullyCombined:
    def test_scope_collection_folder_status_all_together(self):
        sql, params = _build(
            visible_statuses=["published"],
            viewer_scope_profile_ids=["s1"],
            collection_id="c1",
            folder_id="f1",
            include_subfolders=True,
        )
        # 모든 조건이 WHERE 에 AND 로 결합
        assert "d.status IN" in sql
        assert "d.scope_profile_id IN" in sql
        assert "collection_documents" in sql
        assert "path LIKE" in sql
        # 파라미터 순서대로 포함
        assert "published" in params
        assert "s1" in params
        assert "c1" in params
        assert "f1" in params
