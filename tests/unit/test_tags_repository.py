"""S3 Phase 4 FG 4-0 후속: TagsRepository 단위 테스트 (mock 기반).

repositories ≥ 80% 게이트 회복 — Phase 2 FG 2-2 도입 후 unit 테스트 누락 보강.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.models.tag import Tag
from app.repositories.tags_repository import TagsRepository, _row_to_tag


class _Cursor:
    def __init__(self, fetchone_queue=None, fetchall_queue=None, rowcount=0):
        self._fetchone = list(fetchone_queue or [])
        self._fetchall = list(fetchall_queue or [])
        self.rowcount = rowcount
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    def mogrify(self, template, params):
        # cur.mogrify 기본 동작 흉내 — 실제 quoting 은 무시 (테스트 목적)
        rendered = template
        for p in params:
            rendered = rendered.replace("%s", repr(p), 1)
        return rendered.encode()


def _make_conn(fetchone_queue=None, fetchall_queue=None, rowcount=0):
    cur = _Cursor(fetchone_queue, fetchall_queue, rowcount)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def _tag_row(*, tid="t1", name="alpha", usage_count=None) -> dict:
    return {
        "id": tid,
        "name_normalized": name,
        "created_at": datetime(2026, 4, 28),
        "usage_count": usage_count,
    }


# ---------------------------------------------------------------------------
# _row_to_tag
# ---------------------------------------------------------------------------


class TestRowToTag:
    def test_with_usage_count(self):
        t = _row_to_tag(_tag_row(usage_count=5))
        assert t.usage_count == 5

    def test_without_usage_count(self):
        t = _row_to_tag(_tag_row(usage_count=None))
        assert t.usage_count is None

    def test_id_coerced_to_str(self):
        t = _row_to_tag(_tag_row(tid=123))
        assert t.id == "123"


# ---------------------------------------------------------------------------
# upsert_many
# ---------------------------------------------------------------------------


class TestUpsertMany:
    def test_empty_input_returns_empty(self):
        conn, cur = _make_conn()
        result = TagsRepository().upsert_many(conn, [])
        assert result == {}
        assert cur.executed == []

    def test_single_name(self):
        # ON CONFLICT RETURNING id — 한 번 fetchone
        conn, _ = _make_conn(fetchone_queue=[{"id": "tag-1"}])
        result = TagsRepository().upsert_many(conn, ["alpha"])
        assert result == {"alpha": "tag-1"}

    def test_multiple_unique(self):
        conn, _ = _make_conn(
            fetchone_queue=[{"id": "t1"}, {"id": "t2"}],
        )
        result = TagsRepository().upsert_many(conn, ["a", "b"])
        # 순서는 set() 때문에 비결정 — 둘 다 있어야 함
        assert set(result.keys()) == {"a", "b"}

    def test_dedup_via_set(self):
        conn, cur = _make_conn(fetchone_queue=[{"id": "t1"}])
        TagsRepository().upsert_many(conn, ["alpha", "alpha"])
        # 한 번만 INSERT (set 으로 중복 제거)
        assert len(cur.executed) == 1


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    def test_found(self):
        conn, _ = _make_conn(fetchone_queue=[_tag_row()])
        t = TagsRepository().get_by_id(conn, "t1")
        assert t is not None
        assert t.name_normalized == "alpha"

    def test_not_found(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        assert TagsRepository().get_by_id(conn, "missing") is None


# ---------------------------------------------------------------------------
# search_prefix / popular (fetch_many_as 경유)
# ---------------------------------------------------------------------------


class TestSearchPrefix:
    def test_with_q(self):
        conn, cur = _make_conn(fetchall_queue=[[_tag_row(usage_count=3)]])
        result = TagsRepository().search_prefix(conn, q="alp", limit=10)
        assert len(result) == 1
        assert result[0].usage_count == 3
        # SQL 에 LIKE 포함
        assert "LIKE" in cur.executed[0][0]

    def test_without_q(self):
        conn, cur = _make_conn(fetchall_queue=[[_tag_row(usage_count=10)]])
        result = TagsRepository().search_prefix(conn, q=None, limit=10)
        assert len(result) == 1
        # q=None 이면 LIKE 미사용 → 전체 popular 동작
        assert "LIKE" not in cur.executed[0][0]

    def test_limit_clamped(self):
        # max_limit=100 — 200 요청해도 내부에서 clamp
        conn, cur = _make_conn(fetchall_queue=[[]])
        TagsRepository().search_prefix(conn, q="x", limit=999)
        params = cur.executed[0][1]
        assert params[-1] <= 100


class TestPopular:
    def test_returns_tags(self):
        conn, _ = _make_conn(fetchall_queue=[[_tag_row(usage_count=20)]])
        result = TagsRepository().popular(conn, limit=10, min_usage=2)
        assert len(result) == 1
        assert result[0].usage_count == 20

    def test_min_usage_in_params(self):
        conn, cur = _make_conn(fetchall_queue=[[]])
        TagsRepository().popular(conn, limit=10, min_usage=5)
        params = cur.executed[0][1]
        assert 5 in params


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_found(self):
        conn, cur = _make_conn(rowcount=1)
        assert TagsRepository().delete(conn, "t1") is True

    def test_delete_not_found(self):
        conn, cur = _make_conn(rowcount=0)
        assert TagsRepository().delete(conn, "missing") is False


# ---------------------------------------------------------------------------
# replace_for_document
# ---------------------------------------------------------------------------


class TestReplaceForDocument:
    def test_empty_assignments_only_deletes(self):
        conn, cur = _make_conn()
        TagsRepository().replace_for_document(conn, document_id="d1", assignments=[])
        # DELETE 만 실행, INSERT 없음
        assert len(cur.executed) == 1
        assert "DELETE" in cur.executed[0][0]

    def test_with_assignments_inserts(self):
        conn, cur = _make_conn()
        TagsRepository().replace_for_document(
            conn,
            document_id="d1",
            assignments=[("t1", "inline"), ("t2", "frontmatter")],
        )
        # DELETE + INSERT 두 번 실행
        assert len(cur.executed) == 2
        assert "DELETE" in cur.executed[0][0]
        assert "INSERT INTO document_tags" in cur.executed[1][0]


# ---------------------------------------------------------------------------
# list_for_document
# ---------------------------------------------------------------------------


class TestListForDocument:
    def test_returns_tag_source_pairs(self):
        rows = [
            {**_tag_row(name="alpha"), "source": "inline"},
            {**_tag_row(name="beta", tid="t2"), "source": "frontmatter"},
        ]
        conn, _ = _make_conn(fetchall_queue=[rows])
        result = TagsRepository().list_for_document(conn, "d1")
        assert len(result) == 2
        assert result[0][0].name_normalized == "alpha"
        assert result[0][1] == "inline"
        assert result[1][1] == "frontmatter"


# ---------------------------------------------------------------------------
# document_ids_for_tag — ACL 3 분기
# ---------------------------------------------------------------------------


class TestDocumentIdsForTag:
    def test_no_filter_admin_path(self):
        rows = [{"document_id": "d1"}, {"document_id": "d2"}]
        conn, cur = _make_conn(fetchall_queue=[rows])
        result = TagsRepository().document_ids_for_tag(
            conn,
            tag_name_normalized="alpha",
            viewer_scope_profile_ids=None,
        )
        assert result == ["d1", "d2"]
        # scope_profile_id 필터 미포함
        assert "scope_profile_id IN" not in cur.executed[0][0]

    def test_empty_profile_ids_blocks_all(self):
        conn, cur = _make_conn(fetchall_queue=[[]])
        result = TagsRepository().document_ids_for_tag(
            conn,
            tag_name_normalized="alpha",
            viewer_scope_profile_ids=[],
        )
        assert result == []
        # WHERE 1 = 0 으로 차단
        assert "1 = 0" in cur.executed[0][0]

    def test_profile_ids_filter(self):
        rows = [{"document_id": "d1"}]
        conn, cur = _make_conn(fetchall_queue=[rows])
        result = TagsRepository().document_ids_for_tag(
            conn,
            tag_name_normalized="alpha",
            viewer_scope_profile_ids=["sp1", "sp2"],
        )
        assert result == ["d1"]
        assert "scope_profile_id IN" in cur.executed[0][0]
