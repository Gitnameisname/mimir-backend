"""S3 Phase 3 FG 3-1 — contributors_repository 단위 테스트 (cursor stub).

대상: backend/app/repositories/contributors_repository.py

검증 포인트:
  - SQL 쿼리에 EDITOR_EVENT_TYPES / VIEWER_EVENT_TYPE 가 들어감
  - since 파라미터가 있으면 occurred_at >= since 절이 추가됨
  - limit 가 LIMIT 절에 마지막 파라미터로 추가됨
  - actor_user_id IS NULL 행 제외 절이 SQL 에 포함됨
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.contributors_repository import (
    EDITOR_EVENT_TYPES,
    VIEWER_EVENT_TYPE,
    ContributorsRepository,
)


class _CursorStub:
    def __init__(self, fetchone=None, fetchall=None):
        self._fetchone = fetchone
        self._fetchall = fetchall or []
        self.execute_calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, tuple(params) if params else ()))

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


def _conn(stub: _CursorStub) -> MagicMock:
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=stub)
    return conn


# ---------------------------------------------------------------------------
# get_creator
# ---------------------------------------------------------------------------

class TestGetCreator:
    def test_returns_none_when_document_missing(self):
        stub = _CursorStub(fetchone=None)
        result = ContributorsRepository().get_creator(_conn(stub), "doc-1")
        assert result is None
        assert "FROM documents" in stub.execute_calls[0][0]
        assert stub.execute_calls[0][1] == ("doc-1",)

    def test_returns_none_when_created_by_null(self):
        stub = _CursorStub(fetchone={"created_by": None, "created_at": datetime.now(timezone.utc)})
        result = ContributorsRepository().get_creator(_conn(stub), "doc-1")
        assert result is None

    def test_returns_creator_dict(self):
        ts = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
        stub = _CursorStub(fetchone={"created_by": "u-1", "created_at": ts})
        result = ContributorsRepository().get_creator(_conn(stub), "doc-1")
        assert result == {"actor_id": "u-1", "last_activity_at": ts}


# ---------------------------------------------------------------------------
# list_editors
# ---------------------------------------------------------------------------

class TestListEditors:
    def test_passes_event_types_array(self):
        stub = _CursorStub(fetchall=[])
        ContributorsRepository().list_editors(_conn(stub), "doc-1")

        sql, params = stub.execute_calls[0]
        assert "FROM audit_events" in sql
        assert "event_type = ANY" in sql
        assert "actor_user_id IS NOT NULL" in sql
        # params: [document_id, list(EDITOR_EVENT_TYPES), limit]
        assert params[0] == "doc-1"
        assert params[1] == list(EDITOR_EVENT_TYPES)
        assert params[2] == 50  # default limit

    def test_since_adds_filter(self):
        stub = _CursorStub(fetchall=[])
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        ContributorsRepository().list_editors(_conn(stub), "doc-1", since=since)

        sql, params = stub.execute_calls[0]
        assert "occurred_at >= %s" in sql
        # params: [document_id, EDITOR_EVENT_TYPES, since, limit]
        assert params[2] == since

    def test_custom_limit(self):
        stub = _CursorStub(fetchall=[])
        ContributorsRepository().list_editors(_conn(stub), "doc-1", limit=10)

        params = stub.execute_calls[0][1]
        assert params[-1] == 10

    def test_returns_dicts(self):
        stub = _CursorStub(fetchall=[
            {"actor_id": "u-1", "actor_type": "user", "actor_role": "AUTHOR",
             "last_activity_at": datetime(2026, 4, 27, tzinfo=timezone.utc)},
        ])
        rows = ContributorsRepository().list_editors(_conn(stub), "doc-1")
        assert len(rows) == 1
        assert rows[0]["actor_id"] == "u-1"


# ---------------------------------------------------------------------------
# list_approvers
# ---------------------------------------------------------------------------

class TestListApprovers:
    def test_filters_published_status(self):
        stub = _CursorStub(fetchall=[])
        ContributorsRepository().list_approvers(_conn(stub), "doc-1")

        sql, params = stub.execute_calls[0]
        assert "FROM workflow_history" in sql
        assert "to_status = 'published'" in sql
        assert "actor_id IS NOT NULL" in sql
        assert params[0] == "doc-1"

    def test_since_uses_created_at(self):
        stub = _CursorStub(fetchall=[])
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        ContributorsRepository().list_approvers(_conn(stub), "doc-1", since=since)

        sql = stub.execute_calls[0][0]
        assert "created_at >= %s" in sql


# ---------------------------------------------------------------------------
# list_viewers
# ---------------------------------------------------------------------------

class TestListViewers:
    def test_filters_viewed_event_type(self):
        stub = _CursorStub(fetchall=[])
        ContributorsRepository().list_viewers(_conn(stub), "doc-1")

        sql, params = stub.execute_calls[0]
        assert "FROM audit_events" in sql
        assert params[0] == "doc-1"
        assert params[1] == VIEWER_EVENT_TYPE

    def test_since_adds_occurred_at_filter(self):
        stub = _CursorStub(fetchall=[])
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        ContributorsRepository().list_viewers(_conn(stub), "doc-1", since=since)

        sql, params = stub.execute_calls[0]
        assert "occurred_at >= %s" in sql
        assert params[2] == since


# ---------------------------------------------------------------------------
# 모듈 상수
# ---------------------------------------------------------------------------

class TestConstants:
    def test_editor_event_types_8_종(self):
        # FG3-1_audit이벤트_실측.md §2.2 의 7 종 (document.created 포함)
        # creator 와 editors 의 distinct 처리는 service 책임이라 repository 는 7 종 모두 포함
        assert "document.created" in EDITOR_EVENT_TYPES
        assert "document.updated" in EDITOR_EVENT_TYPES
        assert "draft.updated" in EDITOR_EVENT_TYPES
        assert "draft.nodes_saved" in EDITOR_EVENT_TYPES
        assert "draft.discarded" in EDITOR_EVENT_TYPES
        assert "version.created" in EDITOR_EVENT_TYPES
        assert "version.restored" in EDITOR_EVENT_TYPES
        # document.folder_set 은 명시적으로 제외
        assert "document.folder_set" not in EDITOR_EVENT_TYPES

    def test_viewer_event_type(self):
        assert VIEWER_EVENT_TYPE == "document.viewed"
