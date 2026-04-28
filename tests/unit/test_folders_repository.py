"""S3 Phase 4 FG 4-0 후속: FoldersRepository 핵심 경로 단위 테스트.

repositories ≥ 80% 게이트 회복 — Phase 2 FG 2-1 도입 후 unit 테스트 누락 보강.
본 테스트는 read-path (`_row_to_folder` / `get_by_id` / `list_by_owner` /
`create`) 의 mock 회귀만 다룬다. 트리 재계산 (`rename` / `move` / `delete_recursive`)
은 별 라운드의 통합 테스트가 더 적합 — 본 테스트 범위 외.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.repositories.folders_repository import (
    FoldersRepository,
    _row_to_folder,
    compute_child_path,
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


def _make_conn(fetchone_queue=None, fetchall_queue=None):
    cur = _Cursor(fetchone_queue, fetchall_queue)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn, cur


def _folder_row(
    *,
    fid: str = "f1",
    owner_id: str = "u1",
    parent_id=None,
    name: str = "docs",
    path: str = "/docs/",
    depth: int = 0,
) -> dict:
    now = datetime(2026, 4, 28)
    return {
        "id": fid,
        "owner_id": owner_id,
        "parent_id": parent_id,
        "name": name,
        "path": path,
        "depth": depth,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# _row_to_folder
# ---------------------------------------------------------------------------


class TestRowToFolder:
    def test_root_folder(self):
        f = _row_to_folder(_folder_row(parent_id=None))
        assert f.parent_id is None
        assert f.depth == 0
        assert f.path == "/docs/"

    def test_child_folder(self):
        f = _row_to_folder(_folder_row(parent_id="parent-1", depth=1, path="/docs/sub/"))
        assert f.parent_id == "parent-1"
        assert f.depth == 1


# ---------------------------------------------------------------------------
# compute_child_path
# ---------------------------------------------------------------------------


class TestComputeChildPath:
    def test_root_path(self):
        assert compute_child_path(None, "docs") == "/docs/"

    def test_child_path(self):
        assert compute_child_path("/docs/", "sub") == "/docs/sub/"

    def test_slash_in_name_replaced(self):
        assert compute_child_path(None, "a/b") == "/a_b/"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_folder(self):
        conn, cur = _make_conn(fetchone_queue=[_folder_row()])
        f = FoldersRepository().create(
            conn,
            owner_id="u1",
            parent_id=None,
            name="docs",
            path="/docs/",
            depth=0,
        )
        assert f.id == "f1"
        assert f.path == "/docs/"
        assert "INSERT INTO folders" in cur.executed[0][0]


# ---------------------------------------------------------------------------
# get_by_id — owner_id 분기
# ---------------------------------------------------------------------------


class TestGetById:
    def test_found_without_owner(self):
        conn, cur = _make_conn(fetchone_queue=[_folder_row()])
        f = FoldersRepository().get_by_id(conn, "f1")
        assert f is not None
        assert f.id == "f1"
        # owner_id 미사용 → SQL 에 owner_id WHERE 미포함
        assert "owner_id = %s" not in cur.executed[0][0]

    def test_found_with_owner(self):
        conn, cur = _make_conn(fetchone_queue=[_folder_row(owner_id="u1")])
        f = FoldersRepository().get_by_id(conn, "f1", owner_id="u1")
        assert f is not None
        # owner_id 분기 활성화
        assert "owner_id = %s" in cur.executed[0][0]

    def test_not_found(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        assert FoldersRepository().get_by_id(conn, "missing") is None


# ---------------------------------------------------------------------------
# list_by_owner (fetch_many_as 경유)
# ---------------------------------------------------------------------------


class TestListByOwner:
    def test_returns_list(self):
        rows = [
            _folder_row(fid="f1", path="/docs/", depth=0),
            _folder_row(fid="f2", path="/docs/sub/", depth=1, parent_id="f1"),
        ]
        conn, _ = _make_conn(fetchall_queue=[rows])
        result = FoldersRepository().list_by_owner(conn, "u1")
        assert len(result) == 2
        assert result[0].path == "/docs/"
        assert result[1].depth == 1

    def test_empty(self):
        conn, _ = _make_conn(fetchall_queue=[[]])
        assert FoldersRepository().list_by_owner(conn, "u1") == []
