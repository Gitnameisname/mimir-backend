"""
S3 Phase 2 FG 2-2 — /documents?tag=<name> 서버측 필터 회귀.

_build_list_query 가 tag 파라미터를 정규화 + subquery 로 변환하는지 검증.
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


def _build(**filters):
    from app.api.query.models import ParsedListQuery
    from app.repositories.documents_repository import _build_list_query
    return _build_list_query(
        ParsedListQuery(filters=filters, sort_orders=[], page=1, page_size=20),
    )


class TestTagFilter:
    def test_plain_tag(self):
        sql, params, _, _ = _build(tag="ai")
        assert "document_tags" in sql
        assert "t.name_normalized = %s" in sql
        assert "ai" in params

    def test_case_insensitive(self):
        """대소문자 차이 흡수 — `AI` 로 와도 `ai` 로 정규화돼 매칭."""
        sql, params, _, _ = _build(tag="AI")
        assert "ai" in params

    def test_hash_prefix_stripped(self):
        """사용자가 #ai 로 보내도 서버에서 # 제거 + 정규화."""
        sql, params, _, _ = _build(tag="#ai")
        assert "ai" in params

    def test_invalid_tag_skipped(self):
        """정규식 위반 (공백 포함) 은 필터 자체가 생략됨."""
        sql, _, _, _ = _build(tag="sp ace")
        assert "document_tags" not in sql
        assert "WHERE" not in sql  # 다른 필터 없으므로 WHERE 자체 없음

    def test_empty_tag_skipped(self):
        sql, _, _, _ = _build(tag="")
        assert "document_tags" not in sql

    def test_combined_with_collection_and_scope(self):
        from app.api.query.models import ParsedListQuery
        from app.repositories.documents_repository import _build_list_query
        sql, params, _, _ = _build_list_query(
            ParsedListQuery(
                filters={"tag": "foo", "collection": "c1"},
                sort_orders=[],
                page=1,
                page_size=20,
            ),
            viewer_scope_profile_ids=["s1"],
        )
        assert "document_tags" in sql
        assert "collection_documents" in sql
        assert "d.scope_profile_id IN" not in sql  # 얼리어스 없는 형태
        assert "scope_profile_id IN" in sql
        # 파라미터 모두 포함
        assert "foo" in params and "c1" in params and "s1" in params
