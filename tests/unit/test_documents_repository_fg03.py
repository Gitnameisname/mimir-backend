"""
S3 Phase 0 / FG 0-3 후속 S2-B — `app.repositories.documents_repository` 유닛 테스트.

BUG-01 방어 관점:
  - `update_version_pointers` 는 publish / discard_draft 경로의 핵심 커밋 포인트.
    clear_draft / current_draft / current_published 조합 분기를 명확히 검증.

커버:
  - _row_to_document (metadata None, pointers None 분기)
  - _build_list_query (filter / sort / pagination 분기)
  - create / get_by_id / list / update / update_version_pointers
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.repositories.documents_repository import (
    _build_list_query,
    _row_to_document,
    documents_repository,
)

pytestmark = pytest.mark.unit


DOC_ID = "d0000000-0000-0000-0000-000000000001"
_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _doc_row(**kw):
    base = {
        "id": DOC_ID,
        "title": "T",
        "document_type": "policy",
        "status": "draft",
        "metadata": {"k": 1},
        "summary": "s",
        "created_by": "u1",
        "updated_by": "u1",
        "created_at": _NOW,
        "updated_at": _NOW,
        "current_draft_version_id": "v-draft",
        "current_published_version_id": None,
    }
    base.update(kw)
    return base


def _make_conn(
    *,
    fetchone_values: list | None = None,
    fetchall_values: list | None = None,
):
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
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


# --------------------------------------------------------------------------- #
# 1) _row_to_document
# --------------------------------------------------------------------------- #


class TestRowToDocument:
    def test_happy_path(self):
        d = _row_to_document(_doc_row())
        assert d.id == DOC_ID
        assert d.title == "T"
        assert d.metadata == {"k": 1}
        assert d.current_draft_version_id == "v-draft"
        assert d.current_published_version_id is None

    def test_metadata_none_defaults_to_empty_dict(self):
        d = _row_to_document(_doc_row(metadata=None))
        assert d.metadata == {}

    def test_both_pointers_present(self):
        d = _row_to_document(_doc_row(
            current_draft_version_id="draft-x",
            current_published_version_id="pub-y",
        ))
        assert d.current_draft_version_id == "draft-x"
        assert d.current_published_version_id == "pub-y"

    def test_both_pointers_none(self):
        d = _row_to_document(_doc_row(
            current_draft_version_id=None,
            current_published_version_id=None,
        ))
        assert d.current_draft_version_id is None
        assert d.current_published_version_id is None


# --------------------------------------------------------------------------- #
# 2) _build_list_query
# --------------------------------------------------------------------------- #


def _parsed_query(filters=None, sort_orders=None, page=1, page_size=20):
    return SimpleNamespace(
        filters=filters or {},
        sort_orders=sort_orders or [],
        page=page,
        page_size=page_size,
    )


def _sort(field, direction="asc"):
    return SimpleNamespace(field=field, direction=direction)


class TestBuildListQuery:
    def test_no_filters_no_sorts_default_order(self):
        data_sql, data_params, count_sql, count_params = _build_list_query(_parsed_query())
        # 기본 정렬 created_at DESC
        assert "ORDER BY created_at DESC" in data_sql
        # WHERE 절 없음
        assert "WHERE" not in data_sql
        # pagination — limit 20 / offset 0
        assert data_params[-2:] == [20, 0]
        # count params 는 비어있음
        assert count_params == []

    def test_filter_status_and_type_generate_where(self):
        q = _parsed_query(filters={"status": "published", "document_type": "POLICY"})
        data_sql, data_params, count_sql, count_params = _build_list_query(q)
        assert "WHERE" in data_sql
        # status + document_type 두 조건
        assert data_sql.count("= %s") >= 2
        # params 앞 두 개가 필터 값
        assert "published" in data_params
        assert "POLICY" in data_params
        # count_sql 도 동일한 WHERE
        assert "WHERE" in count_sql

    def test_unknown_filter_ignored(self):
        q = _parsed_query(filters={"unknown_field": "x"})
        data_sql, data_params, _, _ = _build_list_query(q)
        assert "WHERE" not in data_sql
        # 파라미터는 pagination 만 (limit + offset = 2)
        assert len(data_params) == 2

    def test_sort_by_title_asc_and_created_at_desc(self):
        q = _parsed_query(sort_orders=[_sort("title", "asc"), _sort("created_at", "desc")])
        data_sql, _, _, _ = _build_list_query(q)
        assert "title ASC" in data_sql
        assert "created_at DESC" in data_sql

    def test_unknown_sort_field_ignored(self):
        q = _parsed_query(sort_orders=[_sort("bogus", "asc")])
        data_sql, _, _, _ = _build_list_query(q)
        # bogus 무시 → 기본 정렬 사용
        assert "bogus" not in data_sql
        assert "ORDER BY created_at DESC" in data_sql

    def test_pagination_offset_calculated(self):
        q = _parsed_query(page=3, page_size=15)
        data_sql, data_params, _, _ = _build_list_query(q)
        # offset = (3-1)*15 = 30
        assert data_params[-2:] == [15, 30]

    def test_negative_page_defaults_to_1(self):
        q = _parsed_query(page=-5, page_size=10)
        _, data_params, _, _ = _build_list_query(q)
        assert data_params[-2:] == [10, 0]

    def test_owner_id_maps_to_created_by_column(self):
        q = _parsed_query(filters={"owner_id": "user-1"})
        data_sql, data_params, _, _ = _build_list_query(q)
        assert "created_by = %s" in data_sql
        assert "user-1" in data_params


# --------------------------------------------------------------------------- #
# 3) CRUD — create / get / list / update
# --------------------------------------------------------------------------- #


class TestCRUD:
    def test_create_inserts_and_serializes_metadata(self):
        conn, cur = _make_conn(fetchone_values=[_doc_row()])
        doc = documents_repository.create(
            conn,
            title="T",
            document_type="policy",
            status="draft",
            metadata={"author": "X"},
            summary="s",
            created_by="u1",
        )
        assert doc.id == DOC_ID
        # metadata 는 JSON 직렬화된 문자열로 파라미터 전달
        params = cur.execute.call_args.args[1]
        assert json.loads(params[3]) == {"author": "X"}
        # created_by == updated_by 둘 다 동일값
        assert params[5] == params[6] == "u1"

    def test_get_by_id_found(self):
        conn, _ = _make_conn(fetchone_values=[_doc_row()])
        doc = documents_repository.get_by_id(conn, DOC_ID)
        assert doc is not None
        assert doc.id == DOC_ID

    def test_get_by_id_none_when_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert documents_repository.get_by_id(conn, "missing") is None

    def test_list_returns_documents_and_total(self):
        q = _parsed_query()
        conn, cur = _make_conn(
            fetchone_values=[{"total": 2}],       # count
            fetchall_values=[[_doc_row(id="a"), _doc_row(id="b")]],
        )
        docs, total = documents_repository.list(conn, q)
        assert total == 2
        assert [d.id for d in docs] == ["a", "b"]
        # execute 호출 2번 — count, list
        assert cur.execute.call_count == 2

    def test_update_title_only_skips_other_fields(self):
        conn, cur = _make_conn(fetchone_values=[_doc_row(title="new")])
        result = documents_repository.update(
            conn, DOC_ID, title="new",
        )
        assert result.title == "new"
        sql = cur.execute.call_args.args[0]
        assert "title = %s" in sql
        assert "status = %s" not in sql
        assert "metadata = %s" not in sql
        assert "summary = %s" not in sql
        assert "updated_by = %s" not in sql
        # WHERE id = %s 로 마지막 param 이 document_id
        params = cur.execute.call_args.args[1]
        assert params[-1] == DOC_ID

    def test_update_all_fields_at_once(self):
        conn, cur = _make_conn(fetchone_values=[_doc_row()])
        documents_repository.update(
            conn, DOC_ID,
            title="t", status="archived",
            metadata={"k": 1}, summary="sum",
            updated_by="u2",
        )
        sql = cur.execute.call_args.args[0]
        for clause in (
            "title = %s",
            "status = %s",
            "metadata = %s",
            "summary = %s",
            "updated_by = %s",
        ):
            assert clause in sql
        # metadata 직렬화
        params = cur.execute.call_args.args[1]
        # 어느 위치인지는 구현에 의존하나, JSON 문자열 존재는 확인
        assert any(isinstance(p, str) and p.startswith('{"k"') for p in params)

    def test_update_returns_none_when_document_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        result = documents_repository.update(conn, "missing", title="x")
        assert result is None


# --------------------------------------------------------------------------- #
# 4) update_version_pointers — BUG-01 방어 핵심
# --------------------------------------------------------------------------- #


class TestUpdateVersionPointers:
    def test_set_published_pointer_only(self):
        """publish 경로: current_published 만 세팅."""
        conn, cur = _make_conn(fetchone_values=[_doc_row(
            current_published_version_id="v-pub",
        )])
        result = documents_repository.update_version_pointers(
            conn, DOC_ID, current_published_version_id="v-pub",
        )
        assert result.current_published_version_id == "v-pub"
        sql = cur.execute.call_args.args[0]
        assert "current_published_version_id = %s" in sql
        # current_draft 는 건들지 않음
        assert "current_draft_version_id = %s" not in sql

    def test_clear_draft_sets_null(self):
        """discard_draft 경로: clear_draft=True 이면 NULL 설정."""
        conn, cur = _make_conn(fetchone_values=[_doc_row(
            current_draft_version_id=None,
        )])
        result = documents_repository.update_version_pointers(
            conn, DOC_ID, clear_draft=True,
        )
        sql = cur.execute.call_args.args[0]
        assert "current_draft_version_id = NULL" in sql
        # clear_draft 는 %s 파라미터를 추가하지 않음
        assert result.current_draft_version_id is None

    def test_publish_pattern_sets_published_and_clears_draft(self):
        """publish 동시 — current_published 세팅 + draft clear."""
        conn, cur = _make_conn(fetchone_values=[_doc_row(
            current_draft_version_id=None,
            current_published_version_id="v-new-pub",
        )])
        result = documents_repository.update_version_pointers(
            conn, DOC_ID,
            current_published_version_id="v-new-pub",
            clear_draft=True,
            updated_by="approver",
        )
        sql = cur.execute.call_args.args[0]
        assert "current_published_version_id = %s" in sql
        assert "current_draft_version_id = NULL" in sql
        assert "updated_by = %s" in sql
        # params: [published_id, updated_by, document_id]
        params = cur.execute.call_args.args[1]
        assert "v-new-pub" in params
        assert "approver" in params
        assert params[-1] == DOC_ID

    def test_set_new_draft_pointer(self):
        """save_draft 새 버전 생성 경로."""
        conn, cur = _make_conn(fetchone_values=[_doc_row()])
        documents_repository.update_version_pointers(
            conn, DOC_ID,
            current_draft_version_id="v-new-draft",
            updated_by="author",
        )
        sql = cur.execute.call_args.args[0]
        assert "current_draft_version_id = %s" in sql
        params = cur.execute.call_args.args[1]
        assert "v-new-draft" in params

    def test_returns_none_when_document_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        result = documents_repository.update_version_pointers(
            conn, "nonexistent", clear_draft=True,
        )
        assert result is None

    def test_updated_at_always_refreshed(self):
        """모든 update_version_pointers 호출에 updated_at = NOW() 포함."""
        conn, cur = _make_conn(fetchone_values=[_doc_row()])
        documents_repository.update_version_pointers(
            conn, DOC_ID, current_draft_version_id="x",
        )
        sql = cur.execute.call_args.args[0]
        assert "updated_at = NOW()" in sql
