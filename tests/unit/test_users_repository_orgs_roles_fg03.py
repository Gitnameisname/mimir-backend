"""
S3 Phase 0 / FG 0-3 후속 S6-A — users_repository 의 OrganizationsRepository + RolesRepository 유닛.

세션 5 에서 UsersRepository 만 커버했으므로 나머지 2개 클래스를 마저 처리.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.users_repository import (
    OrganizationsRepository,
    RolesRepository,
    _row_to_organization,
    _row_to_role,
)

pytestmark = pytest.mark.unit


ORG_ID = "org-0000-0000-0000-0000-000000000001"
ROLE_ID = "role-0000-0000-0000-0000-000000000001"
_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _org_row(**kw):
    base = {
        "id": ORG_ID, "name": "Acme",
        "description": "corporate", "status": "ACTIVE",
        "created_at": _NOW, "updated_at": _NOW,
    }
    base.update(kw)
    return base


def _role_row(**kw):
    base = {
        "id": ROLE_ID, "name": "AUTHOR", "description": "글 작성자",
        "is_system": False, "created_at": _NOW,
    }
    base.update(kw)
    return base


def _make_conn(*, fetchone_values=None, fetchall_values=None, rowcount=0):
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
# OrganizationsRepository
# --------------------------------------------------------------------------- #


class TestOrganizationsRepository:
    @pytest.fixture
    def repo(self):
        return OrganizationsRepository()

    def test_row_to_organization(self):
        o = _row_to_organization(_org_row())
        assert o.id == ORG_ID
        assert o.name == "Acme"

    def test_get_by_id_found(self, repo):
        conn, _ = _make_conn(fetchone_values=[_org_row()])
        assert repo.get_by_id(conn, ORG_ID).id == ORG_ID

    def test_get_by_id_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        assert repo.get_by_id(conn, ORG_ID) is None

    def test_list_no_filter(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 2}],
            fetchall_values=[[_org_row(id="o1"), _org_row(id="o2")]],
        )
        orgs, total = repo.list(conn)
        assert total == 2
        assert [o.id for o in orgs] == ["o1", "o2"]
        for c in cur.execute.call_args_list:
            assert "WHERE" not in c.args[0]

    def test_list_with_search(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 1}],
            fetchall_values=[[_org_row()]],
        )
        repo.list(conn, search="acme")
        sql = cur.execute.call_args_list[0].args[0]
        assert "name ILIKE %s" in sql
        assert "WHERE" in sql

    def test_list_with_status_and_search(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 0}], fetchall_values=[[]],
        )
        repo.list(conn, search="x", status="ACTIVE", limit=5, offset=10)
        sql = cur.execute.call_args_list[0].args[0]
        assert "status = %s" in sql
        assert "name ILIKE" in sql
        # data_sql 파라미터: [search, status, limit, offset]
        last_params = cur.execute.call_args_list[1].args[1]
        assert last_params[-2:] == [5, 10]

    def test_create(self, repo):
        conn, cur = _make_conn(fetchone_values=[_org_row()])
        org = repo.create(conn, name="N", description="D", status="ACTIVE")
        assert org.id == ORG_ID
        params = cur.execute.call_args.args[1]
        assert params == ("N", "D", "ACTIVE")

    def test_update_partial(self, repo):
        conn, cur = _make_conn(fetchone_values=[_org_row(name="New")])
        org = repo.update(conn, ORG_ID, name="New")
        assert org.name == "New"
        sql = cur.execute.call_args.args[0]
        assert "name = %s" in sql
        assert "description = %s" not in sql
        assert "status = %s" not in sql
        assert "updated_at = NOW()" in sql

    def test_update_all_fields(self, repo):
        conn, cur = _make_conn(fetchone_values=[_org_row()])
        repo.update(conn, ORG_ID, name="N", description="D", status="SUSPENDED")
        sql = cur.execute.call_args.args[0]
        for clause in ("name = %s", "description = %s", "status = %s", "updated_at = NOW()"):
            assert clause in sql

    def test_update_empty_falls_back_to_get_by_id(self, repo):
        """모든 파라미터 None 이면 SELECT 로 폴백."""
        conn, cur = _make_conn(fetchone_values=[_org_row()])
        org = repo.update(conn, ORG_ID)
        assert org is not None
        # SELECT 한 번만
        assert cur.execute.call_count == 1
        assert "SELECT" in cur.execute.call_args.args[0]
        assert "UPDATE" not in cur.execute.call_args.args[0]

    def test_update_returns_none_when_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        assert repo.update(conn, ORG_ID, name="x") is None

    def test_delete_success(self, repo):
        conn, _ = _make_conn(rowcount=1)
        assert repo.delete(conn, ORG_ID) is True

    def test_delete_not_found(self, repo):
        conn, _ = _make_conn(rowcount=0)
        assert repo.delete(conn, ORG_ID) is False


# --------------------------------------------------------------------------- #
# RolesRepository
# --------------------------------------------------------------------------- #


class TestRolesRepository:
    @pytest.fixture
    def repo(self):
        return RolesRepository()

    def test_row_to_role(self):
        r = _row_to_role(_role_row(is_system=True))
        assert r.name == "AUTHOR"
        assert r.is_system is True

    def test_get_by_id_found(self, repo):
        conn, _ = _make_conn(fetchone_values=[_role_row()])
        assert repo.get_by_id(conn, ROLE_ID).id == ROLE_ID

    def test_get_by_id_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        assert repo.get_by_id(conn, ROLE_ID) is None

    def test_get_by_name(self, repo):
        conn, cur = _make_conn(fetchone_values=[_role_row()])
        repo.get_by_name(conn, "AUTHOR")
        sql = cur.execute.call_args.args[0]
        assert "name = %s" in sql

    def test_list_orders_system_first_then_name(self, repo):
        rows = [_role_row(id="r1"), _role_row(id="r2", is_system=True)]
        conn, cur = _make_conn(fetchall_values=[rows])
        result = repo.list(conn)
        assert len(result) == 2
        sql = cur.execute.call_args.args[0]
        assert "ORDER BY is_system DESC, name" in sql

    def test_create_is_system_always_false(self, repo):
        conn, cur = _make_conn(fetchone_values=[_role_row()])
        r = repo.create(conn, name="CUSTOM", description="D")
        assert r.id == ROLE_ID
        sql = cur.execute.call_args.args[0]
        # is_system 이 FALSE 로 하드코드
        assert "is_system" in sql
        assert "FALSE" in sql

    def test_update_only_when_not_system(self, repo):
        conn, cur = _make_conn(fetchone_values=[_role_row(description="new")])
        r = repo.update(conn, ROLE_ID, description="new")
        assert r.description == "new"
        sql = cur.execute.call_args.args[0]
        # is_system = FALSE 가드가 SQL 에 포함
        assert "is_system = FALSE" in sql

    def test_update_returns_none_when_system_or_missing(self, repo):
        """is_system=TRUE 이거나 부재 시 UPDATE ... RETURNING 의 row 는 없음."""
        conn, _ = _make_conn(fetchone_values=[None])
        result = repo.update(conn, ROLE_ID, description="x")
        assert result is None

    def test_delete_only_when_not_system(self, repo):
        conn, cur = _make_conn(rowcount=1)
        result = repo.delete(conn, ROLE_ID)
        assert result is True
        sql = cur.execute.call_args.args[0]
        assert "is_system = FALSE" in sql

    def test_delete_system_role_returns_false(self, repo):
        """DELETE … AND is_system = FALSE — 시스템 역할은 영향받지 않아 rowcount=0."""
        conn, _ = _make_conn(rowcount=0)
        assert repo.delete(conn, ROLE_ID) is False
