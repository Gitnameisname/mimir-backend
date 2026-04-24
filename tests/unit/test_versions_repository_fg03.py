"""
S3 Phase 0 / FG 0-3 후속 S3-A — `app.repositories.versions_repository` 유닛 테스트.

커버 대상:
  - _row_to_version (metadata None, parent/restored None 분기)
  - get_next_version_number
  - create (전체 파라미터 직렬화 확인)
  - get_by_id / get_by_document_and_version_id (found/missing)
  - get_active_draft / get_current_published
  - update_status (status-only / published_by 포함 / missing)
  - update_content (부분 필드 / 전체 필드 / 빈 업데이트 → get_by_id 폴백)
  - list_by_document_id (sort whitelist + pagination)
  - delete (성공/실패)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.versions_repository import (
    _row_to_version,
    versions_repository,
)

pytestmark = pytest.mark.unit


DOC_ID = "doc-00000000-0000-0000-0000-000000000001"
VER_ID = "ver-00000000-0000-0000-0000-000000000001"
_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _ver_row(**kw):
    base = {
        "id": VER_ID, "document_id": DOC_ID,
        "version_number": 1, "label": "initial",
        "status": "draft", "change_summary": "first",
        "source": "manual", "metadata": {"k": 1},
        "created_by": "u1", "created_at": _NOW,
        "parent_version_id": None, "restored_from_version_id": None,
        "title_snapshot": "t", "summary_snapshot": "s",
        "metadata_snapshot": None, "content_snapshot": {"type": "document"},
        "published_by": None, "published_at": None,
    }
    base.update(kw)
    return base


def _make_conn(*, fetchone_values=None, fetchall_values=None, rowcount=1):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(side_effect=list(fetchone_values)) if fetchone_values is not None else MagicMock(return_value=None)
    cur.fetchall = MagicMock(side_effect=list(fetchall_values)) if fetchall_values is not None else MagicMock(return_value=[])
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


# --------------------------------------------------------------------------- #
# 1) _row_to_version
# --------------------------------------------------------------------------- #


class TestRowToVersion:
    def test_happy_path(self):
        v = _row_to_version(_ver_row())
        assert v.id == VER_ID
        assert v.status == "draft"
        assert v.metadata == {"k": 1}
        assert v.parent_version_id is None
        assert v.restored_from_version_id is None

    def test_metadata_none_defaults_to_empty(self):
        v = _row_to_version(_ver_row(metadata=None))
        assert v.metadata == {}

    def test_parent_and_restored_pointers(self):
        v = _row_to_version(_ver_row(
            parent_version_id="parent-x",
            restored_from_version_id="past-y",
        ))
        assert v.parent_version_id == "parent-x"
        assert v.restored_from_version_id == "past-y"


# --------------------------------------------------------------------------- #
# 2) get_next_version_number
# --------------------------------------------------------------------------- #


class TestGetNextVersionNumber:
    def test_first_version_starts_from_1(self):
        conn, _ = _make_conn(fetchone_values=[{"next_number": 1}])
        assert versions_repository.get_next_version_number(conn, DOC_ID) == 1

    def test_increments_from_existing_max(self):
        conn, _ = _make_conn(fetchone_values=[{"next_number": 5}])
        assert versions_repository.get_next_version_number(conn, DOC_ID) == 5


# --------------------------------------------------------------------------- #
# 3) create
# --------------------------------------------------------------------------- #


class TestCreate:
    def test_serializes_metadata_and_snapshots(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        v = versions_repository.create(
            conn,
            document_id=DOC_ID, version_number=1,
            label="v1", status="draft",
            change_summary="initial", source="manual",
            metadata={"a": 1},
            created_by="u1",
            content_snapshot={"type": "document"},
        )
        assert v.id == VER_ID
        params = cur.execute.call_args.args[1]
        # metadata JSON 문자열
        assert json.loads(params[6]) == {"a": 1}
        # content_snapshot JSON 문자열 (마지막 파라미터)
        assert json.loads(params[13]) == {"type": "document"}

    def test_metadata_snapshot_none_remains_none(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        versions_repository.create(
            conn,
            document_id=DOC_ID, version_number=1,
            label=None, status="draft",
            change_summary=None, source="manual",
            metadata={},
            created_by=None,
            metadata_snapshot=None,     # None → NULL 로 전달
            content_snapshot=None,
        )
        params = cur.execute.call_args.args[1]
        # metadata_snapshot 은 None (인덱스 12), content_snapshot None (인덱스 13)
        assert params[12] is None
        assert params[13] is None


# --------------------------------------------------------------------------- #
# 4) get_by_id / get_by_document_and_version_id / get_active_draft / get_current_published
# --------------------------------------------------------------------------- #


class TestGetByVariants:
    def test_get_by_id_found(self):
        conn, _ = _make_conn(fetchone_values=[_ver_row()])
        v = versions_repository.get_by_id(conn, VER_ID)
        assert v is not None and v.id == VER_ID

    def test_get_by_id_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert versions_repository.get_by_id(conn, VER_ID) is None

    def test_get_by_document_and_version_id_enforces_ownership(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        versions_repository.get_by_document_and_version_id(conn, DOC_ID, VER_ID)
        sql = cur.execute.call_args.args[0]
        assert "id = %s" in sql
        assert "document_id = %s" in sql
        # 파라미터 순서 (version_id, document_id)
        params = cur.execute.call_args.args[1]
        assert params == (VER_ID, DOC_ID)

    def test_get_by_document_and_version_id_not_found(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert versions_repository.get_by_document_and_version_id(conn, DOC_ID, VER_ID) is None

    def test_get_active_draft_found(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row(status="draft")])
        v = versions_repository.get_active_draft(conn, DOC_ID)
        assert v is not None
        sql = cur.execute.call_args.args[0]
        assert "status = 'draft'" in sql
        assert "ORDER BY version_number DESC" in sql
        assert "LIMIT 1" in sql

    def test_get_active_draft_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert versions_repository.get_active_draft(conn, DOC_ID) is None

    def test_get_current_published_found(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row(status="published")])
        v = versions_repository.get_current_published(conn, DOC_ID)
        assert v is not None
        sql = cur.execute.call_args.args[0]
        assert "status = 'published'" in sql

    def test_get_current_published_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert versions_repository.get_current_published(conn, DOC_ID) is None


# --------------------------------------------------------------------------- #
# 5) update_status
# --------------------------------------------------------------------------- #


class TestUpdateStatus:
    def test_status_only(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row(status="superseded")])
        v = versions_repository.update_status(conn, VER_ID, status="superseded")
        assert v.status == "superseded"
        sql = cur.execute.call_args.args[0]
        assert "status = %s" in sql
        # published_by / published_at 은 set 에 포함되지 않음
        assert "published_by = %s" not in sql
        assert "published_at = %s" not in sql
        # 파라미터: (status, version_id)
        params = cur.execute.call_args.args[1]
        assert params == ["superseded", VER_ID]

    def test_publish_with_user_and_timestamp(self):
        pub_at = datetime(2026, 4, 23, 15, 0, tzinfo=timezone.utc)
        conn, cur = _make_conn(fetchone_values=[_ver_row(
            status="published", published_by="approver", published_at=pub_at,
        )])
        v = versions_repository.update_status(
            conn, VER_ID,
            status="published", published_by="approver", published_at=pub_at,
        )
        assert v.status == "published"
        sql = cur.execute.call_args.args[0]
        for clause in ("status = %s", "published_by = %s", "published_at = %s"):
            assert clause in sql
        # 마지막 파라미터는 WHERE id = %s
        params = cur.execute.call_args.args[1]
        assert params[-1] == VER_ID

    def test_missing_version_returns_none(self):
        conn, _ = _make_conn(fetchone_values=[None])
        result = versions_repository.update_status(conn, VER_ID, status="discarded")
        assert result is None


# --------------------------------------------------------------------------- #
# 6) update_content
# --------------------------------------------------------------------------- #


class TestUpdateContent:
    def test_updates_all_fields(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        versions_repository.update_content(
            conn, VER_ID,
            label="new-label", change_summary="new-summary",
            title_snapshot="new-title", summary_snapshot="new-sum",
            metadata_snapshot={"m": 1}, content_snapshot={"type": "document"},
        )
        sql = cur.execute.call_args.args[0]
        for clause in (
            "label = %s", "change_summary = %s",
            "title_snapshot = %s", "summary_snapshot = %s",
            "metadata_snapshot = %s", "content_snapshot = %s",
        ):
            assert clause in sql
        # snapshot 들은 JSON 직렬화되어 전달
        params = cur.execute.call_args.args[1]
        assert any(isinstance(p, str) and '"type"' in p for p in params)

    def test_partial_update(self):
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        versions_repository.update_content(
            conn, VER_ID, label="only-label",
        )
        sql = cur.execute.call_args.args[0]
        assert "label = %s" in sql
        # 다른 필드 clause 는 포함되지 않음
        for clause in (
            "change_summary = %s", "title_snapshot = %s",
            "summary_snapshot = %s", "metadata_snapshot = %s",
            "content_snapshot = %s",
        ):
            assert clause not in sql

    def test_empty_update_falls_back_to_get_by_id(self):
        """모든 파라미터 None 이면 UPDATE 실행 없이 get_by_id 반환."""
        conn, cur = _make_conn(fetchone_values=[_ver_row()])
        result = versions_repository.update_content(conn, VER_ID)
        assert result is not None
        # SELECT 만 1회 호출 (get_by_id)
        assert cur.execute.call_count == 1
        sql = cur.execute.call_args.args[0]
        assert "SELECT" in sql
        assert "UPDATE" not in sql

    def test_returns_none_when_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        result = versions_repository.update_content(
            conn, VER_ID, label="x",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# 7) list_by_document_id
# --------------------------------------------------------------------------- #


class TestListByDocumentId:
    def test_default_sort_version_number_desc(self):
        rows = [_ver_row(id="v1"), _ver_row(id="v2")]
        conn, cur = _make_conn(
            fetchone_values=[{"total": 2}],
            fetchall_values=[rows],
        )
        items, total = versions_repository.list_by_document_id(conn, DOC_ID)
        assert total == 2
        assert [v.id for v in items] == ["v1", "v2"]
        sql = cur.execute.call_args_list[1].args[0]
        assert "ORDER BY version_number DESC" in sql

    def test_sort_by_created_at_asc(self):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 0}], fetchall_values=[[]],
        )
        versions_repository.list_by_document_id(
            conn, DOC_ID, sort_field="created_at", sort_dir="ASC",
        )
        sql = cur.execute.call_args_list[1].args[0]
        assert "ORDER BY created_at ASC" in sql

    def test_unknown_sort_field_falls_back_to_whitelist_default(self):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 0}], fetchall_values=[[]],
        )
        versions_repository.list_by_document_id(
            conn, DOC_ID, sort_field="malicious; DROP TABLE", sort_dir="DESC",
        )
        sql = cur.execute.call_args_list[1].args[0]
        # 화이트리스트 통과 기본: version_number
        assert "ORDER BY version_number" in sql
        # 악성 문자열은 SQL 에 들어가지 않음
        assert "DROP" not in sql

    def test_pagination_offset(self):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 5}], fetchall_values=[[]],
        )
        versions_repository.list_by_document_id(
            conn, DOC_ID, page=3, page_size=10,
        )
        params = cur.execute.call_args_list[1].args[1]
        # (document_id, page_size, offset) — offset = (3-1)*10 = 20
        assert params == (DOC_ID, 10, 20)


# --------------------------------------------------------------------------- #
# 8) delete
# --------------------------------------------------------------------------- #


class TestDelete:
    def test_delete_success(self):
        conn, cur = _make_conn(rowcount=1)
        result = versions_repository.delete(conn, VER_ID)
        assert result is True

    def test_delete_not_found(self):
        conn, cur = _make_conn(rowcount=0)
        result = versions_repository.delete(conn, VER_ID)
        assert result is False
