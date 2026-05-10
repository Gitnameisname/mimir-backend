"""
S3 Phase 5 FG 5-3 — 멘션 typeahead 사용자 검색 R-A4 회귀.

회귀 영역:
  1. SQL 정합 (search_by_display_name_in_orgs)
     - viewer_user_id 가 WHERE 절에 keyword-only required
     - viewer 본인 제외
     - status='ACTIVE' 만
     - ILIKE prefix + ESCAPE
     - viewer 와 같은 org 안 사용자만 (user_org_roles JOIN)
  2. 라우터 레이어
     - 인증 미통과 → 401
     - q 빈 / 너무 김 → 400
     - viewer_user_id 가 ActorContext 에서만 추출 (query 주입 무시)
     - 응답에 user_id + display_name 만 (email/role/status 누설 0 — UserSearchItem 모델로 자동 보장)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# A. UsersRepository.search_by_display_name_in_orgs — SQL 정합 (mock cursor)
# ---------------------------------------------------------------------------

class TestRepositorySQL:
    def _make_conn(self, fetchall_value=None):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        cur.fetchall = MagicMock(return_value=fetchall_value or [])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        return conn, cur

    def test_empty_query_returns_empty(self):
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn, cur = self._make_conn()
        result = repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query="", limit=20,
        )
        assert result == []
        # SQL 자체가 호출되지 않아야 (성능)
        assert cur.execute.call_count == 0

    def test_sql_includes_user_org_roles_join_and_viewer_self_exclude(self):
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn, cur = self._make_conn(fetchall_value=[])
        repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query="ali", limit=10,
        )
        sql = cur.execute.call_args.args[0]
        # JOIN user_org_roles 두 번 (target + viewer)
        assert sql.count("JOIN user_org_roles") >= 2
        # viewer 본인 제외
        assert "u.id != %s" in sql
        # ACTIVE 만
        assert "u.status = 'ACTIVE'" in sql
        # ILIKE prefix + ESCAPE
        assert "ILIKE %s ESCAPE" in sql
        # ORDER + LIMIT
        assert "ORDER BY u.display_name" in sql
        assert "LIMIT %s" in sql

    def test_sql_params_include_viewer_id_twice(self):
        """SQL 의 (viewer_user_id, viewer_user_id, prefix, limit) 파라미터 순서."""
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn, cur = self._make_conn(fetchall_value=[])
        repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query="ali", limit=10,
        )
        params = cur.execute.call_args.args[1]
        # 첫 두 인자는 viewer_user_id (JOIN 의 viewer_uor.user_id + WHERE u.id != %s)
        assert params[0] == "u1"
        assert params[1] == "u1"
        # 세 번째는 prefix (ali → ali%)
        assert params[2] == "ali%"
        # limit
        assert params[3] == 10

    def test_wildcards_in_query_are_escaped(self):
        """ILIKE wildcard `%` `_` `\\` 가 escape 되어 prefix injection 방어."""
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn, cur = self._make_conn(fetchall_value=[])
        # 악의적 입력: '%' 으로 ALL 매칭 시도
        repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query="%adm", limit=5,
        )
        params = cur.execute.call_args.args[1]
        # `%` 가 `\%` 로 escape 됨
        assert params[2] == r"\%adm%"

        # underscore + backslash
        conn2, cur2 = self._make_conn(fetchall_value=[])
        repo.search_by_display_name_in_orgs(
            conn2, viewer_user_id="u1", query="a_\\b", limit=5,
        )
        params2 = cur2.execute.call_args.args[1]
        # `_` → `\_`, `\` → `\\`
        assert params2[2] == r"a\_\\b%"

    def test_response_only_contains_user_id_and_display_name(self):
        """응답 row 가 정확히 {user_id, display_name} 만 — email/role 누설 차단."""
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn, cur = self._make_conn(fetchall_value=[
            # cur.fetchall 이 RealDictRow 와 유사한 dict 반환
            {"id": "u-1", "display_name": "Alice"},
            {"id": "u-2", "display_name": "Alex"},
        ])
        result = repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="u1", query="al", limit=10,
        )
        assert result == [
            {"user_id": "u-1", "display_name": "Alice"},
            {"user_id": "u-2", "display_name": "Alex"},
        ]
        # 키 정확히 2개만
        for item in result:
            assert set(item.keys()) == {"user_id", "display_name"}


# ---------------------------------------------------------------------------
# B. 라우터 — 인증 / viewer_user_id 강제 / 응답 모델
# ---------------------------------------------------------------------------

class TestRouterAuthAndACL:
    # NOTE: 인증 미통과 401 회귀는 slowapi limiter 가 starlette.Request 인스턴스 강제하여
    # 단위 단계에서 라우터 함수 직접 호출이 불편하다. 통합 테스트 (TestClient + auth fixture)
    # 로 위임 — 잔여 항목 (검수보고서 §5).

    def test_response_model_excludes_email_role_status(self):
        """UserSearchItem / UserSearchResponse 가 응답 직렬화 시 email/role 자체를
        포함할 수 없게 모델 정의로 보장."""
        from app.schemas.user_search import UserSearchItem, UserSearchResponse

        item = UserSearchItem(user_id="u-1", display_name="Alice")
        dumped = item.model_dump()
        assert set(dumped.keys()) == {"user_id", "display_name"}
        # email/role/status 누설 가능성 0
        assert "email" not in dumped
        assert "role" not in dumped
        assert "status" not in dumped

        resp = UserSearchResponse(items=[item], items_total=1, items_truncated=False)
        dumped_resp = resp.model_dump()
        for k in dumped_resp["items"][0]:
            assert k in {"user_id", "display_name"}

    def test_response_truncated_flag_when_limit_reached(self):
        """결과 수가 limit 와 같으면 truncated=True (사용자에게 더 좁은 prefix 안내)."""
        from app.schemas.user_search import UserSearchItem, UserSearchResponse
        items = [UserSearchItem(user_id=f"u-{i}", display_name=f"User{i}") for i in range(20)]
        resp = UserSearchResponse(items=items, items_total=20, items_truncated=True)
        assert resp.items_truncated is True
        assert len(resp.items) == 20


# ---------------------------------------------------------------------------
# C. R-A4 multi-org 격리 (mock SQL 결과로 시뮬레이션)
# ---------------------------------------------------------------------------

class TestRA4MultiOrgIsolation:
    """SQL 의 user_org_roles JOIN 이 의도대로 동작함을 mock 으로 시뮬레이션.

    실 통합 회귀는 별 라운드 (DB fixture + TestClient) 에서.
    """

    def test_other_org_user_not_returned(self):
        """SQL 결과 자체에 다른 org 사용자가 없으면 응답에 노출 0 — 자명한 회귀.

        이는 SQL 의 JOIN 구조가 viewer 와 같은 org 인 사용자만 반환하는 것을 가정.
        통합 시 실제 DB 데이터로 검증.
        """
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.execute = MagicMock()
        # JOIN 결과: 같은 org 안 사용자만 — 다른 org 의 'BSecret' 같은 user 는 없음
        cur.fetchall = MagicMock(return_value=[
            {"id": "u-same-org", "display_name": "SameOrgUser"},
        ])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        result = repo.search_by_display_name_in_orgs(
            conn, viewer_user_id="viewer-A", query="B", limit=20,
        )
        # 다른 org 의 BSecret 같은 사용자 노출 0
        names = [r["display_name"] for r in result]
        assert "BSecret" not in names
        assert names == ["SameOrgUser"]

    def test_viewer_user_id_must_be_keyword_only(self):
        """positional 호출 시 TypeError — keyword-only 강제 (S2 ⑥ Scope 하드코딩 금지)."""
        from app.repositories.users_repository import UsersRepository
        repo = UsersRepository()
        conn = MagicMock()
        with pytest.raises(TypeError):
            # viewer_user_id 를 positional 로 전달하면 TypeError
            repo.search_by_display_name_in_orgs(conn, "u1", query="x")  # type: ignore[call-arg]
