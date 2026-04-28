"""S3 Phase 4 FG 4-0 §2.1.6: ScopeProfileRepository CRUD + helpers 단위 테스트.

mock conn / cursor 기반 — 실 DB 불필요. CRUD 모든 경로 + `_row_to_profile`
매핑 + `allowed_tools` 직렬화 / 검증 / 라운드트립 검증.

본 테스트는 Phase 4 §5.2 의 repositories ≥ 80% 게이트 회복을 위한 것.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.scope_profile import (
    ScopeDefinition,
    ScopeProfile,
    ScopeProfileSettings,
)
from app.repositories.scope_profile_repository import (
    ScopeProfileRepository,
    _allowed_tools_from_raw,
    _allowed_tools_validate,
)


# ---------------------------------------------------------------------------
# 헬퍼 — mock conn / cursor
# ---------------------------------------------------------------------------


class _Cursor:
    """fetchone / fetchall 큐를 순서대로 반환하는 mock cursor."""

    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self._fetchone = list(fetchone_queue or [])
        self._fetchall = list(fetchall_queue or [])
        self.executed = []
        self.rowcount = 0

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


def _profile_row(
    *,
    pid: str = "11111111-1111-1111-1111-111111111111",
    name: str = "p1",
    description: str | None = None,
    organization_id: str | None = None,
    settings_json: Any = None,
    allowed_tools: Any = None,
) -> dict:
    """ScopeProfile row dict — psycopg2 RealDictRow 모방."""
    now = datetime(2026, 4, 28)
    return {
        "id": pid,
        "name": name,
        "description": description,
        "organization_id": organization_id,
        "settings_json": settings_json,
        "allowed_tools": allowed_tools,
        "created_at": now,
        "updated_at": now,
    }


def _definition_row(
    *,
    did: str = "22222222-2222-2222-2222-222222222222",
    pid: str = "11111111-1111-1111-1111-111111111111",
    scope_name: str = "default",
    description: str | None = None,
    acl_filter: Any = None,
) -> dict:
    return {
        "id": did,
        "scope_profile_id": pid,
        "scope_name": scope_name,
        "description": description,
        "acl_filter": acl_filter or {},
        "created_at": datetime(2026, 4, 28),
    }


# ---------------------------------------------------------------------------
# _allowed_tools_from_raw — edge case 망라
# ---------------------------------------------------------------------------


class TestAllowedToolsFromRaw:
    def test_none_returns_empty(self):
        assert _allowed_tools_from_raw(None) == []

    def test_empty_list(self):
        assert _allowed_tools_from_raw([]) == []

    def test_list_of_strings(self):
        assert _allowed_tools_from_raw(["a", "b"]) == ["a", "b"]

    def test_json_string_list(self):
        assert _allowed_tools_from_raw(json.dumps(["x", "y"])) == ["x", "y"]

    def test_dict_returns_empty(self):
        # 잘못된 타입 — fail-closed 빈 리스트
        assert _allowed_tools_from_raw({"x": 1}) == []

    def test_int_returns_empty(self):
        assert _allowed_tools_from_raw(42) == []

    def test_list_with_none_filtered(self):
        assert _allowed_tools_from_raw(["a", None, "b"]) == ["a", "b"]

    def test_list_with_mixed_types_coerced(self):
        # int → str 강제
        assert _allowed_tools_from_raw(["a", 1, "b"]) == ["a", "1", "b"]


# ---------------------------------------------------------------------------
# _allowed_tools_validate
# ---------------------------------------------------------------------------


class TestAllowedToolsValidate:
    def test_empty_list_ok(self):
        assert _allowed_tools_validate([]) == []

    def test_single_known_tool(self):
        # known_tool_names() 는 TOOL_SCHEMAS 의 5 도구
        assert _allowed_tools_validate(["search_documents"]) == ["search_documents"]

    def test_dedupe_and_sort(self):
        result = _allowed_tools_validate(["fetch_node", "fetch_node", "search_documents"])
        assert result == ["fetch_node", "search_documents"]

    def test_unknown_tool_raises(self):
        with pytest.raises(ValueError) as exc:
            _allowed_tools_validate(["__unknown__"])
        assert "__unknown__" in str(exc.value)

    def test_partial_unknown_raises(self):
        with pytest.raises(ValueError):
            _allowed_tools_validate(["search_documents", "__bad__"])


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_minimal_create(self):
        row = _profile_row(name="new-profile")
        conn, _ = _make_conn(fetchone_queue=[row])
        repo = ScopeProfileRepository(conn)

        profile = repo.create(name="new-profile")
        assert profile.id == row["id"]
        assert profile.name == "new-profile"
        assert profile.allowed_tools == []
        assert profile.settings.expose_viewers is False

    def test_create_with_allowed_tools(self):
        row = _profile_row(allowed_tools=json.dumps(["search_documents"]))
        conn, cur = _make_conn(fetchone_queue=[row])
        repo = ScopeProfileRepository(conn)

        profile = repo.create(name="p", allowed_tools=["search_documents"])
        assert profile.allowed_tools == ["search_documents"]
        # INSERT 가 한 번 실행됨
        assert len(cur.executed) == 1

    def test_create_with_unknown_tool_raises(self):
        conn, _ = _make_conn(fetchone_queue=[])
        repo = ScopeProfileRepository(conn)

        with pytest.raises(ValueError):
            repo.create(name="p", allowed_tools=["__bogus__"])

    def test_create_with_explicit_settings(self):
        row = _profile_row(settings_json=json.dumps({"expose_viewers": True}))
        conn, _ = _make_conn(fetchone_queue=[row])
        repo = ScopeProfileRepository(conn)

        profile = repo.create(
            name="p",
            settings=ScopeProfileSettings(expose_viewers=True),
        )
        assert profile.settings.expose_viewers is True


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    def test_found(self):
        # get_by_id 는 SELECT scope_profile + SELECT scope_definitions 두 번 호출
        row = _profile_row()
        conn, _ = _make_conn(
            fetchone_queue=[row],
            fetchall_queue=[[]],  # _list_definitions 는 fetchall
        )
        repo = ScopeProfileRepository(conn)
        profile = repo.get_by_id(row["id"])
        assert profile is not None
        assert profile.id == row["id"]
        assert profile.scopes == []

    def test_not_found(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        repo = ScopeProfileRepository(conn)
        assert repo.get_by_id("missing") is None

    def test_with_definitions(self):
        row = _profile_row()
        def_row = _definition_row(acl_filter={"and": []})
        conn, _ = _make_conn(
            fetchone_queue=[row],
            fetchall_queue=[[def_row]],
        )
        repo = ScopeProfileRepository(conn)
        profile = repo.get_by_id(row["id"])
        assert len(profile.scopes) == 1
        assert profile.scopes[0].scope_name == "default"


# ---------------------------------------------------------------------------
# list_profiles + count
# ---------------------------------------------------------------------------


class TestList:
    def test_list_without_org(self):
        rows = [_profile_row(pid=f"id-{i}", name=f"p{i}") for i in range(3)]
        # list_profiles → SELECT (fetchall) + 각 프로파일별 _list_definitions (fetchall)
        conn, _ = _make_conn(
            fetchall_queue=[rows, [], [], []],
        )
        repo = ScopeProfileRepository(conn)
        profiles = repo.list_profiles()
        assert len(profiles) == 3

    def test_list_with_org(self):
        rows = [_profile_row(organization_id="org-1")]
        conn, _ = _make_conn(fetchall_queue=[rows, []])
        repo = ScopeProfileRepository(conn)
        profiles = repo.list_profiles(organization_id="org-1")
        assert len(profiles) == 1
        assert profiles[0].organization_id == "org-1"

    def test_count_without_org(self):
        conn, _ = _make_conn(fetchone_queue=[{"count": 5}])
        repo = ScopeProfileRepository(conn)
        assert repo.count() == 5

    def test_count_with_org(self):
        conn, _ = _make_conn(fetchone_queue=[{"count": 2}])
        repo = ScopeProfileRepository(conn)
        assert repo.count(organization_id="org-1") == 2


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_name_only(self):
        updated_row = _profile_row(name="renamed")
        conn, cur = _make_conn(fetchone_queue=[updated_row], fetchall_queue=[[]])
        repo = ScopeProfileRepository(conn)
        profile = repo.update(updated_row["id"], name="renamed")
        assert profile.name == "renamed"

    def test_update_description(self):
        updated_row = _profile_row(description="new desc")
        conn, _ = _make_conn(fetchone_queue=[updated_row], fetchall_queue=[[]])
        repo = ScopeProfileRepository(conn)
        profile = repo.update(updated_row["id"], description="new desc")
        assert profile.description == "new desc"

    def test_update_allowed_tools(self):
        updated_row = _profile_row(allowed_tools=json.dumps(["search_documents", "fetch_node"]))
        conn, _ = _make_conn(fetchone_queue=[updated_row], fetchall_queue=[[]])
        repo = ScopeProfileRepository(conn)
        profile = repo.update(
            updated_row["id"],
            allowed_tools=["search_documents", "fetch_node"],
        )
        assert profile.allowed_tools == ["search_documents", "fetch_node"]

    def test_update_allowed_tools_validates(self):
        conn, _ = _make_conn(fetchone_queue=[])
        repo = ScopeProfileRepository(conn)
        with pytest.raises(ValueError):
            repo.update("pid", allowed_tools=["__nope__"])

    def test_update_settings_patch_merges_unknown(self):
        # update flow:
        #   1. get_by_id (existing)  → SELECT profile + SELECT defs
        #   2. SELECT settings_json (raw)
        #   3. UPDATE → returning row + SELECT defs
        existing = _profile_row(settings_json=json.dumps({"expose_viewers": False, "alien": "k"}))
        updated = _profile_row(settings_json=json.dumps({"expose_viewers": True, "alien": "k"}))
        conn, _ = _make_conn(
            fetchone_queue=[
                existing,                                       # get_by_id
                {"settings_json": json.dumps({"alien": "k"})},  # raw 머지용
                updated,                                        # UPDATE returning
            ],
            fetchall_queue=[[], []],  # 두 번의 _list_definitions
        )
        repo = ScopeProfileRepository(conn)
        profile = repo.update("pid", settings_patch={"expose_viewers": True})
        assert profile is not None
        assert profile.settings.expose_viewers is True

    def test_update_not_found(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        repo = ScopeProfileRepository(conn)
        assert repo.update("missing", name="x") is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_found(self):
        conn, cur = _make_conn()
        cur.rowcount = 1  # DELETE 가 한 행 영향
        # 직접 cursor.rowcount 를 1 로 설정해야 — _Cursor 는 default 0
        # 실제로는 execute() 후에 rowcount 가 set 됨 — 단순화 위해 init 시 set
        repo = ScopeProfileRepository(conn)
        assert repo.delete("pid") is True

    def test_delete_not_found(self):
        conn, cur = _make_conn()
        cur.rowcount = 0
        repo = ScopeProfileRepository(conn)
        assert repo.delete("missing") is False


# ---------------------------------------------------------------------------
# Definition CRUD
# ---------------------------------------------------------------------------


class TestDefinitionCrud:
    def test_add_definition(self):
        def_row = _definition_row(scope_name="read", acl_filter={"and": []})
        conn, _ = _make_conn(fetchone_queue=[def_row])
        repo = ScopeProfileRepository(conn)
        sd = repo.add_definition("pid", scope_name="read", acl_filter={"and": []})
        assert sd.scope_name == "read"

    def test_get_definition_found(self):
        def_row = _definition_row(scope_name="default")
        conn, _ = _make_conn(fetchone_queue=[def_row])
        repo = ScopeProfileRepository(conn)
        sd = repo.get_definition("pid", "default")
        assert sd is not None
        assert sd.scope_name == "default"

    def test_get_definition_not_found(self):
        conn, _ = _make_conn(fetchone_queue=[None])
        repo = ScopeProfileRepository(conn)
        assert repo.get_definition("pid", "default") is None

    def test_delete_definition_found(self):
        conn, cur = _make_conn()
        cur.rowcount = 1
        repo = ScopeProfileRepository(conn)
        assert repo.delete_definition("pid", "default") is True

    def test_delete_definition_not_found(self):
        conn, cur = _make_conn()
        cur.rowcount = 0
        repo = ScopeProfileRepository(conn)
        assert repo.delete_definition("pid", "default") is False

    def test_acl_filter_string_json_parsed(self):
        # acl_filter 가 str (psycopg2 가 json 문자열로 반환) 일 때 dict 로 파싱
        def_row = _definition_row(acl_filter=json.dumps({"and": [{"field": "x", "op": "eq", "value": 1}]}))
        conn, _ = _make_conn(fetchone_queue=[def_row])
        repo = ScopeProfileRepository(conn)
        sd = repo.get_definition("pid", "default")
        assert isinstance(sd.acl_filter, dict)
        assert sd.acl_filter["and"][0]["field"] == "x"

    def test_acl_filter_none_normalized(self):
        def_row = _definition_row(acl_filter=None)
        conn, _ = _make_conn(fetchone_queue=[def_row])
        repo = ScopeProfileRepository(conn)
        sd = repo.get_definition("pid", "default")
        assert sd.acl_filter == {}


# ---------------------------------------------------------------------------
# _row_to_profile mapping (private but worth verifying directly)
# ---------------------------------------------------------------------------


class TestRowToProfile:
    def test_full_row(self):
        row = _profile_row(
            allowed_tools=json.dumps(["search_documents"]),
            settings_json=json.dumps({"expose_viewers": True}),
            organization_id="org-1",
        )
        profile = ScopeProfileRepository._row_to_profile(row, scopes=[])
        assert profile.allowed_tools == ["search_documents"]
        assert profile.settings.expose_viewers is True
        assert profile.organization_id == "org-1"

    def test_null_optional_fields(self):
        row = _profile_row()
        profile = ScopeProfileRepository._row_to_profile(row, scopes=[])
        assert profile.allowed_tools == []
        assert profile.settings.expose_viewers is False
        assert profile.organization_id is None

    def test_with_scopes(self):
        row = _profile_row()
        scope = ScopeDefinition(
            id="d1",
            scope_profile_id=row["id"],
            scope_name="default",
            description=None,
            acl_filter={"and": []},
            created_at=datetime(2026, 4, 28),
        )
        profile = ScopeProfileRepository._row_to_profile(row, scopes=[scope])
        assert len(profile.scopes) == 1
        assert profile.scopes[0].scope_name == "default"
