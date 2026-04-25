"""Unit tests for :mod:`app.db.cursor_helpers`.

Covers:
    - fetch_one_as: row 있음 → mapper / row 없음 → None.
    - fetch_many_as: 다수 row / 빈 결과 → [].
    - mapper 예외 전파.
    - DB 드라이버 예외 (execute 실패) 전파.
    - cursor context manager close 보장.
Docs: ``docs/함수도서관/backend.md`` §1.8 BE-G5.
"""
from __future__ import annotations

import pytest

from app.db.cursor_helpers import fetch_many_as, fetch_one_as


class _FakeCursor:
    """psycopg2 cursor 의 최소 호환 mock (with-context + execute + fetch*)."""

    def __init__(self, rows: list, raise_on_execute: Exception | None = None):
        self._rows = rows
        self._raise = raise_on_execute
        self.executed: tuple | None = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def execute(self, sql, params):
        if self._raise is not None:
            raise self._raise
        self.executed = (sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


# ---------------------------------------------------------------------------
# fetch_one_as
# ---------------------------------------------------------------------------


class TestFetchOneAs:
    def test_row_found_applies_mapper(self):
        cur = _FakeCursor([{"id": "x", "name": "a"}])
        conn = _FakeConn(cur)
        result = fetch_one_as(
            conn, "SELECT * FROM t WHERE id=%s", ("x",), lambda r: r["name"]
        )
        assert result == "a"
        assert cur.executed == ("SELECT * FROM t WHERE id=%s", ("x",))
        assert cur.closed

    def test_no_row_returns_none(self):
        cur = _FakeCursor([])
        conn = _FakeConn(cur)
        result = fetch_one_as(conn, "SELECT 1", (), lambda r: r)
        assert result is None
        assert cur.closed

    def test_mapper_returning_none_passes_through(self):
        # row 가 있으면 mapper(row) 결과를 그대로 반환 (None 도 valid)
        cur = _FakeCursor([{"id": "x"}])
        conn = _FakeConn(cur)
        result = fetch_one_as(conn, "SELECT 1", (), lambda r: None)
        assert result is None

    def test_mapper_exception_propagates(self):
        cur = _FakeCursor([{"id": "x"}])
        conn = _FakeConn(cur)
        with pytest.raises(KeyError):
            fetch_one_as(conn, "SELECT 1", (), lambda r: r["missing"])

    def test_execute_exception_propagates(self):
        cur = _FakeCursor([], raise_on_execute=RuntimeError("DB down"))
        conn = _FakeConn(cur)
        with pytest.raises(RuntimeError, match="DB down"):
            fetch_one_as(conn, "SELECT 1", (), lambda r: r)

    def test_dict_params_supported(self):
        cur = _FakeCursor([{"x": 1}])
        conn = _FakeConn(cur)
        fetch_one_as(conn, "SELECT 1 WHERE id=%(id)s", {"id": "x"}, lambda r: r)
        assert cur.executed == ("SELECT 1 WHERE id=%(id)s", {"id": "x"})


# ---------------------------------------------------------------------------
# fetch_many_as
# ---------------------------------------------------------------------------


class TestFetchManyAs:
    def test_multiple_rows_mapped(self):
        rows = [{"n": 1}, {"n": 2}, {"n": 3}]
        cur = _FakeCursor(rows)
        conn = _FakeConn(cur)
        result = fetch_many_as(conn, "SELECT n", (), lambda r: r["n"] * 10)
        assert result == [10, 20, 30]
        assert cur.closed

    def test_empty_result_returns_empty_list(self):
        cur = _FakeCursor([])
        conn = _FakeConn(cur)
        result = fetch_many_as(conn, "SELECT 1", (), lambda r: r)
        assert result == []

    def test_mapper_can_return_complex_types(self):
        from collections import namedtuple

        Item = namedtuple("Item", ["id", "name"])
        rows = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        cur = _FakeCursor(rows)
        conn = _FakeConn(cur)
        result = fetch_many_as(conn, "SELECT id,name", (), lambda r: Item(**r))
        assert result == [Item("a", "A"), Item("b", "B")]

    def test_mapper_exception_propagates(self):
        cur = _FakeCursor([{"id": "x"}, {"id": "y"}])
        conn = _FakeConn(cur)
        with pytest.raises(KeyError):
            fetch_many_as(conn, "SELECT 1", (), lambda r: r["missing"])
