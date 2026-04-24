"""
S3 Phase 0 / FG 0-3 후속 S5-A — `app.repositories.users_repository` 유닛 테스트.

대상: UsersRepository 클래스 핵심.
OrganizationsRepository / RolesRepository 는 세션 6+ 로 이월 (별도 큰 범위).

커버:
  - _row_to_user / _row_to_user_org_role (변환 + scope_profile_id 폴백)
  - get_by_id / get_by_email / get_by_username (대소문자 무관) / get_by_identifier (이메일 vs username)
  - list (search / status / role_name 필터 + pagination + 조합)
  - create (모든 파라미터 + 기본값)
  - update (개별 필드 + 빈 업데이트 → get_by_id 폴백 + sentinel 패턴 locked_until)
  - record_login_success / record_login_failure
  - delete (성공/실패)
  - assign_org_role (upsert) / remove_org_role / get_user_role_in_org
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.users_repository import (
    _row_to_user,
    _row_to_user_org_role,
    UsersRepository,
)

pytestmark = pytest.mark.unit


USER_ID = "user-00000000-0000-0000-0000-000000000001"
ORG_ID = "org-0000-0000-0000-0000-000000000001"
_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _user_row(**kw):
    base = {
        "id": USER_ID, "email": "u@example.com", "display_name": "U",
        "status": "ACTIVE", "role_name": "VIEWER",
        "last_login_at": None, "created_at": _NOW, "updated_at": _NOW,
        "username": "u1", "password_hash": "hash",
        "auth_provider": "local", "email_verified": True,
        "email_verified_at": _NOW, "failed_login_count": 0,
        "locked_until": None, "avatar_url": None,
        "scope_profile_id": "profile-1",
    }
    base.update(kw)
    return base


def _user_org_role_row(**kw):
    base = {
        "id": "uor-1", "user_id": USER_ID, "org_id": ORG_ID,
        "role_name": "AUTHOR", "created_at": _NOW,
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


@pytest.fixture
def repo():
    return UsersRepository()


# --------------------------------------------------------------------------- #
# 1) _row_to_user / _row_to_user_org_role
# --------------------------------------------------------------------------- #


class TestRowHelpers:
    def test_row_to_user_happy(self):
        u = _row_to_user(_user_row())
        assert u.id == USER_ID
        assert u.email == "u@example.com"
        assert u.scope_profile_id == "profile-1"

    def test_row_to_user_scope_profile_none(self):
        u = _row_to_user(_user_row(scope_profile_id=None))
        assert u.scope_profile_id is None

    def test_row_to_user_missing_optional_fields_default(self):
        """구버전 DB 에 scope_profile_id / avatar_url 컬럼 없을 때 — 키 부재."""
        row = _user_row()
        row.pop("scope_profile_id")   # 키 자체 제거
        row.pop("avatar_url")
        u = _row_to_user(row)
        # 키 부재도 None 폴백
        assert u.scope_profile_id is None
        assert u.avatar_url is None

    def test_row_to_user_org_role(self):
        r = _row_to_user_org_role(_user_org_role_row())
        assert r.user_id == USER_ID
        assert r.role_name == "AUTHOR"


# --------------------------------------------------------------------------- #
# 2) get_by_* / get_by_identifier
# --------------------------------------------------------------------------- #


class TestGetByVariants:
    def test_get_by_id_found(self, repo):
        conn, _ = _make_conn(fetchone_values=[_user_row()])
        u = repo.get_by_id(conn, USER_ID)
        assert u is not None and u.id == USER_ID

    def test_get_by_id_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        assert repo.get_by_id(conn, "missing") is None

    def test_get_by_email(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.get_by_email(conn, "u@example.com")
        sql = cur.execute.call_args.args[0]
        assert "email = %s" in sql

    def test_get_by_username_case_insensitive(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.get_by_username(conn, "U1")
        sql = cur.execute.call_args.args[0]
        # LOWER 두 번 사용 (DB 컬럼과 인자 양쪽)
        assert "LOWER(username) = LOWER(%s)" in sql

    def test_get_by_identifier_email_routes_to_email(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.get_by_identifier(conn, "Test@Example.COM")
        # email 쿼리 사용 + 소문자 정규화
        sql = cur.execute.call_args.args[0]
        assert "email = %s" in sql
        params = cur.execute.call_args.args[1]
        assert params == ("test@example.com",)

    def test_get_by_identifier_username_routes_to_username(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.get_by_identifier(conn, "  myname  ")
        # username 쿼리 — 공백 trim
        sql = cur.execute.call_args.args[0]
        assert "LOWER(username)" in sql
        params = cur.execute.call_args.args[1]
        assert params == ("myname",)


# --------------------------------------------------------------------------- #
# 3) list
# --------------------------------------------------------------------------- #


class TestList:
    def test_no_filters_default_pagination(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 2}],
            fetchall_values=[[_user_row(id="u1"), _user_row(id="u2")]],
        )
        users, total = repo.list(conn)
        assert total == 2
        assert [u.id for u in users] == ["u1", "u2"]
        # 두 SQL 모두 WHERE 절 없음
        for c in cur.execute.call_args_list:
            assert "WHERE" not in c.args[0]
        # 마지막 SQL 의 LIMIT/OFFSET params
        last_params = cur.execute.call_args_list[1].args[1]
        assert last_params == [20, 0]

    def test_search_filter_applies_ilike(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 1}], fetchall_values=[[_user_row()]],
        )
        repo.list(conn, search="kim")
        sql = cur.execute.call_args_list[0].args[0]
        assert "WHERE" in sql
        assert "display_name ILIKE" in sql
        assert "email ILIKE" in sql
        params = cur.execute.call_args_list[0].args[1]
        assert params[0] == "%kim%" and params[1] == "%kim%"

    def test_status_and_role_filters(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 0}], fetchall_values=[[]],
        )
        repo.list(conn, status="ACTIVE", role_name="ADMIN")
        sql = cur.execute.call_args_list[0].args[0]
        assert "status = %s" in sql
        assert "role_name = %s" in sql

    def test_combined_search_status_role(self, repo):
        conn, cur = _make_conn(
            fetchone_values=[{"total": 0}], fetchall_values=[[]],
        )
        repo.list(conn, search="lee", status="SUSPENDED", role_name="VIEWER",
                  limit=50, offset=10)
        sql = cur.execute.call_args_list[0].args[0]
        # 3 조건 AND
        assert sql.count("%s") >= 4   # search 2 + status 1 + role 1
        assert "AND" in sql
        last_params = cur.execute.call_args_list[1].args[1]
        # 필터 파라미터 4개 + LIMIT/OFFSET 2개 = 6
        assert last_params[-2:] == [50, 10]


# --------------------------------------------------------------------------- #
# 4) create / update
# --------------------------------------------------------------------------- #


class TestCreate:
    def test_create_with_all_params(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        u = repo.create(
            conn,
            email="x@example.com", display_name="X",
            role_name="AUTHOR", status="PENDING",
            password_hash="$2b$...", auth_provider="local",
            email_verified=False, avatar_url="/a.png",
            username="xname",
        )
        assert u is not None
        # 9개 positional parameter (INSERT 문 순서)
        params = cur.execute.call_args.args[1]
        assert len(params) == 9
        assert params[0] == "x@example.com"
        assert params[2] == "AUTHOR"
        assert params[5] == "local"
        assert params[8] == "xname"

    def test_create_default_role_and_status(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.create(conn, email="e@x.com", display_name="E")
        params = cur.execute.call_args.args[1]
        # 기본값 role_name="VIEWER", status="ACTIVE"
        assert params[2] == "VIEWER"
        assert params[3] == "ACTIVE"
        # auth_provider 기본 "local"
        assert params[5] == "local"


class TestUpdate:
    def test_update_single_field(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row(display_name="New")])
        u = repo.update(conn, USER_ID, display_name="New")
        assert u.display_name == "New"
        sql = cur.execute.call_args.args[0]
        assert "display_name = %s" in sql
        assert "updated_at = NOW()" in sql
        # 다른 필드 SET 없음
        assert "role_name = %s" not in sql
        assert "password_hash = %s" not in sql

    def test_update_multiple_fields(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        from datetime import datetime, timezone
        repo.update(
            conn, USER_ID,
            role_name="ADMIN", status="SUSPENDED",
            password_hash="new-hash", email_verified=True,
            failed_login_count=3,
            locked_until=datetime(2026, 4, 24, tzinfo=timezone.utc),
            last_login_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
            avatar_url="/a.png", username="new",
        )
        sql = cur.execute.call_args.args[0]
        for clause in (
            "role_name = %s", "status = %s", "password_hash = %s",
            "email_verified = %s", "failed_login_count = %s",
            "locked_until = %s", "last_login_at = %s",
            "avatar_url = %s", "username = %s", "updated_at = NOW()",
        ):
            assert clause in sql

    def test_update_no_fields_falls_back_to_get_by_id(self, repo):
        """모든 파라미터 None → UPDATE 없이 get_by_id 실행."""
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        u = repo.update(conn, USER_ID)   # 아무 것도 변경 안함
        assert u is not None
        # SELECT * FROM users WHERE id = %s 한 번
        assert cur.execute.call_count == 1
        sql = cur.execute.call_args.args[0]
        assert "SELECT" in sql
        assert "UPDATE" not in sql

    def test_update_returns_none_when_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        u = repo.update(conn, "missing", display_name="X")
        assert u is None

    def test_update_locked_until_none_is_not_applied(self, repo):
        """locked_until=None 이면 SET clause 에 포함되지 않음 (sentinel 아닌 기본값 매칭)."""
        conn, cur = _make_conn(fetchone_values=[_user_row()])
        repo.update(conn, USER_ID, display_name="X", locked_until=None)
        sql = cur.execute.call_args.args[0]
        assert "locked_until" not in sql
        # display_name 만 SET 됨
        assert "display_name = %s" in sql


# --------------------------------------------------------------------------- #
# 5) record_login_success / record_login_failure
# --------------------------------------------------------------------------- #


class TestRecordLogin:
    def test_success_resets_failed_count_and_unlocks(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row(failed_login_count=0)])
        repo.record_login_success(conn, USER_ID)
        sql = cur.execute.call_args.args[0]
        assert "failed_login_count = 0" in sql
        assert "locked_until = NULL" in sql
        assert "last_login_at = NOW()" in sql

    def test_success_returns_none_when_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        assert repo.record_login_success(conn, "missing") is None

    def test_failure_increments_failed_count(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_row(failed_login_count=1)])
        repo.record_login_failure(conn, USER_ID)
        sql = cur.execute.call_args.args[0]
        assert "failed_login_count = failed_login_count + 1" in sql


# --------------------------------------------------------------------------- #
# 6) delete
# --------------------------------------------------------------------------- #


class TestDelete:
    def test_delete_success(self, repo):
        conn, _ = _make_conn(rowcount=1)
        assert repo.delete(conn, USER_ID) is True

    def test_delete_not_found(self, repo):
        conn, _ = _make_conn(rowcount=0)
        assert repo.delete(conn, USER_ID) is False


# --------------------------------------------------------------------------- #
# 7) org role mapping
# --------------------------------------------------------------------------- #


class TestOrgRoleMapping:
    def test_assign_org_role_upserts(self, repo):
        conn, cur = _make_conn(fetchone_values=[_user_org_role_row()])
        r = repo.assign_org_role(conn, user_id=USER_ID, org_id=ORG_ID, role_name="AUTHOR")
        assert r.role_name == "AUTHOR"
        sql = cur.execute.call_args.args[0]
        assert "INSERT INTO user_org_roles" in sql
        assert "ON CONFLICT (user_id, org_id, role_name) DO UPDATE" in sql

    def test_remove_org_role_success(self, repo):
        conn, _ = _make_conn(rowcount=1)
        assert repo.remove_org_role(conn, user_id=USER_ID, org_id=ORG_ID) is True

    def test_remove_org_role_not_found(self, repo):
        conn, _ = _make_conn(rowcount=0)
        assert repo.remove_org_role(conn, user_id=USER_ID, org_id=ORG_ID) is False

    def test_get_user_role_in_org_found(self, repo):
        conn, _ = _make_conn(fetchone_values=[{"role_name": "AUTHOR"}])
        role = repo.get_user_role_in_org(conn, USER_ID, ORG_ID)
        assert role == "AUTHOR"

    def test_get_user_role_in_org_missing(self, repo):
        conn, _ = _make_conn(fetchone_values=[None])
        role = repo.get_user_role_in_org(conn, USER_ID, ORG_ID)
        assert role is None
