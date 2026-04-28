"""S3 Phase 4 FG 4-0 후속: NotificationsRepository 단위 테스트 (mock 기반).

repositories ≥ 80% 게이트 회복 — Phase 3 FG 3-3 도입 후 unit 테스트 누락 보강.
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.repositories.notifications_repository import (
    NotificationsRepository,
    _row_to_notification,
)


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


def _make_conn(fetchone_queue=None, fetchall_queue=None, rowcount=0):
    cur = _Cursor(fetchone_queue, fetchall_queue, rowcount)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def _row(
    *,
    nid: str = "n1",
    user_id: str = "u1",
    kind: str = "mention",
    payload=None,
    read_at=None,
) -> dict:
    return {
        "id": nid,
        "user_id": user_id,
        "kind": kind,
        "payload": payload,
        "read_at": read_at,
        "created_at": datetime(2026, 4, 28),
    }


# ---------------------------------------------------------------------------
# _row_to_notification
# ---------------------------------------------------------------------------


class TestRowToNotification:
    def test_payload_dict_preserved(self):
        n = _row_to_notification(_row(payload={"k": "v"}))
        assert n.payload == {"k": "v"}

    def test_payload_json_string_parsed(self):
        n = _row_to_notification(_row(payload=json.dumps({"k": "v"})))
        assert n.payload == {"k": "v"}

    def test_payload_none_normalized_to_empty(self):
        n = _row_to_notification(_row(payload=None))
        assert n.payload == {}

    def test_payload_invalid_json_returns_empty(self):
        # loads_maybe 가 list 반환 → dict 가 아니므로 빈 dict
        n = _row_to_notification(_row(payload=[1, 2, 3]))
        assert n.payload == {}

    def test_id_coerced_to_str(self):
        n = _row_to_notification(_row(nid=123))
        assert n.id == "123"


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_returns_notification(self):
        conn, _ = _make_conn(fetchone_queue=[_row(payload={"x": 1})])
        n = NotificationsRepository().enqueue(
            conn, user_id="u1", kind="mention", payload={"x": 1}
        )
        assert n.user_id == "u1"
        assert n.kind == "mention"
        assert n.payload == {"x": 1}

    def test_enqueue_with_empty_payload(self):
        conn, _ = _make_conn(fetchone_queue=[_row(payload={})])
        n = NotificationsRepository().enqueue(
            conn, user_id="u1", kind="mention", payload={}
        )
        assert n.payload == {}


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


class TestListForUser:
    def test_all(self):
        conn, _ = _make_conn(fetchall_queue=[[_row(nid="a"), _row(nid="b")]])
        items = NotificationsRepository().list_for_user(conn, "u1")
        assert len(items) == 2

    def test_unread_only(self):
        conn, cur = _make_conn(fetchall_queue=[[_row(read_at=None)]])
        items = NotificationsRepository().list_for_user(conn, "u1", unread_only=True)
        assert len(items) == 1
        # SQL 에 "read_at IS NULL" 포함 확인
        assert "IS NULL" in cur.executed[0][0]

    def test_limit_applied(self):
        conn, cur = _make_conn(fetchall_queue=[[]])
        NotificationsRepository().list_for_user(conn, "u1", limit=10)
        # params 끝에 limit 가 들어감
        params = cur.executed[0][1]
        assert params[-1] == 10


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_empty_list_short_circuits(self):
        conn, cur = _make_conn()
        n = NotificationsRepository().mark_read(conn, "u1", [])
        assert n == 0
        # SQL 미실행
        assert cur.executed == []

    def test_marks_with_rowcount(self):
        conn, cur = _make_conn(rowcount=3)
        n = NotificationsRepository().mark_read(conn, "u1", ["a", "b", "c"])
        assert n == 3

    def test_zero_rowcount(self):
        conn, cur = _make_conn(rowcount=0)
        n = NotificationsRepository().mark_read(conn, "u1", ["x"])
        assert n == 0


# ---------------------------------------------------------------------------
# count_unread + count_recent_per_pair
# ---------------------------------------------------------------------------


class TestCounts:
    def test_count_unread(self):
        conn, _ = _make_conn(fetchone_queue=[{"c": 7}])
        assert NotificationsRepository().count_unread(conn, "u1") == 7

    def test_count_unread_empty(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        assert NotificationsRepository().count_unread(conn, "u1") == 0

    def test_count_recent_per_pair(self):
        conn, _ = _make_conn(fetchone_queue=[{"c": 4}])
        n = NotificationsRepository().count_recent_per_pair(
            conn, author_id="a1", recipient_id="r1", within_seconds=60
        )
        assert n == 4

    def test_count_recent_pair_empty(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        n = NotificationsRepository().count_recent_per_pair(
            conn, author_id="a", recipient_id="r", within_seconds=60
        )
        assert n == 0
