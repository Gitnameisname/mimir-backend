"""
S3 Phase 2 FG 2-1 UX 3차 회귀 — documents?q=<str> 서버 측 제목 검색.

_build_list_query 가 q 필터를 안전하게 ILIKE 조건으로 변환하고, LIKE 메타 문자
(`%`, `_`) 와 백슬래시를 이스케이프하는지 확인한다.
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


def Q(**filters):
    from app.api.query.models import ParsedListQuery
    return ParsedListQuery(
        filters=filters, sort_orders=[], page=1, page_size=20,
    )


class TestQFilter:
    def test_plain_keyword(self):
        from app.repositories.documents_repository import _build_list_query
        sql, params, _, _ = _build_list_query(Q(q="정책"))
        assert "title ILIKE %s ESCAPE" in sql
        assert "%정책%" in params

    def test_whitespace_only_is_skipped(self):
        from app.repositories.documents_repository import _build_list_query
        sql, params, _, _ = _build_list_query(Q(q="   "))
        assert "title ILIKE" not in sql
        assert "WHERE" not in sql

    def test_empty_string_skipped(self):
        from app.repositories.documents_repository import _build_list_query
        sql, _, _, _ = _build_list_query(Q(q=""))
        assert "title ILIKE" not in sql

    def test_percent_is_escaped(self):
        """'%' 를 쓰면 사용자가 임의 LIKE 매칭을 하지 못하도록 이스케이프."""
        from app.repositories.documents_repository import _build_list_query
        _sql, params, _, _ = _build_list_query(Q(q="100%"))
        # escape 된 % 가 패턴 중간에 포함되어야 하고, 양 끝의 wildcard % 는 유지
        assert any("100\\%" in p for p in params if isinstance(p, str))

    def test_underscore_is_escaped(self):
        from app.repositories.documents_repository import _build_list_query
        _sql, params, _, _ = _build_list_query(Q(q="a_b"))
        assert any("a\\_b" in p for p in params if isinstance(p, str))

    def test_backslash_is_escaped(self):
        from app.repositories.documents_repository import _build_list_query
        _sql, params, _, _ = _build_list_query(Q(q="a\\b"))
        assert any("a\\\\b" in p for p in params if isinstance(p, str))

    def test_combined_with_other_filters(self):
        from app.repositories.documents_repository import _build_list_query
        sql, params, _, _ = _build_list_query(
            Q(q="정책", status="draft"),
            viewer_scope_profile_ids=["s1"],
        )
        assert "title ILIKE" in sql
        assert "status = %s" in sql
        assert "scope_profile_id IN" in sql
        # 순서 무관하게 세 값 모두 포함
        assert "draft" in params
        assert "s1" in params
        assert any("정책" in (p or "") for p in params if isinstance(p, str))

    def test_non_string_q_ignored(self):
        """q 가 str 이 아니면 skip — whitelist 기반 안전."""
        from app.repositories.documents_repository import _build_list_query
        sql, _, _, _ = _build_list_query(Q(q=123))  # type: ignore[arg-type]
        assert "title ILIKE" not in sql
