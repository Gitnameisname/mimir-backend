"""S3 Phase 1 FG 1-3 — users_repository.get_preferences / update_preferences 유닛.

커버 대상 분기:
  * get_preferences: row 부재 / None / dict / str JSON / 잘못된 JSON / 컬럼 부재 예외
  * update_preferences: 빈 패치 / 키 추가 / 키 갱신 / null 로 키 삭제 / str/dict 반환 케이스
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.repositories.users_repository import users_repository

pytestmark = pytest.mark.unit


USER_ID = "11111111-1111-4111-8111-111111111111"


def _conn_with_fetchone(values: list):
    """execute 호출 순서대로 fetchone 이 values 를 반환하는 mock conn."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.side_effect = values
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def _conn_raising_execute(exc: Exception):
    """execute 호출 시 즉시 예외를 던지는 mock conn — 컬럼 부재 시뮬레이션."""
    conn = MagicMock()
    cur = MagicMock()
    cur.execute.side_effect = exc
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


# --------------------------------------------------------------------------- #
# get_preferences
# --------------------------------------------------------------------------- #


class TestGetPreferences:
    def test_row_none_returns_empty_dict(self):
        conn, _ = _conn_with_fetchone([None])
        assert users_repository.get_preferences(conn, USER_ID) == {}

    def test_preferences_none_returns_empty_dict(self):
        conn, _ = _conn_with_fetchone([{"preferences": None}])
        assert users_repository.get_preferences(conn, USER_ID) == {}

    def test_preferences_dict_returns_as_is(self):
        prefs = {"editor_view_mode": "flow", "theme": "dark"}
        conn, _ = _conn_with_fetchone([{"preferences": prefs}])
        assert users_repository.get_preferences(conn, USER_ID) == prefs

    def test_preferences_json_string_is_parsed(self):
        prefs = {"editor_view_mode": "block"}
        conn, _ = _conn_with_fetchone([{"preferences": json.dumps(prefs)}])
        assert users_repository.get_preferences(conn, USER_ID) == prefs

    def test_preferences_invalid_json_string_returns_empty(self):
        conn, _ = _conn_with_fetchone([{"preferences": "not-valid-json"}])
        assert users_repository.get_preferences(conn, USER_ID) == {}

    def test_preferences_non_dict_non_str_returns_empty(self):
        conn, _ = _conn_with_fetchone([{"preferences": 42}])
        assert users_repository.get_preferences(conn, USER_ID) == {}

    def test_column_absent_falls_back_to_empty(self):
        """컬럼이 없는 레거시 DB — psycopg2 ProgrammingError 던질 때 빈 dict 반환."""
        import psycopg2
        conn, _ = _conn_raising_execute(psycopg2.ProgrammingError("column does not exist"))
        result = users_repository.get_preferences(conn, USER_ID)
        assert result == {}
        conn.rollback.assert_called_once()


# --------------------------------------------------------------------------- #
# update_preferences — shallow merge + null 삭제
# --------------------------------------------------------------------------- #


class TestUpdatePreferences:
    def test_empty_current_add_new_key(self):
        # get_preferences 1회 + update 후 fetchone 1회 = 총 2회 fetchone
        conn, cur = _conn_with_fetchone([
            {"preferences": {}},                                # get_preferences
            {"preferences": {"editor_view_mode": "flow"}},      # update RETURNING
        ])
        result = users_repository.update_preferences(
            conn, USER_ID, {"editor_view_mode": "flow"}
        )
        assert result == {"editor_view_mode": "flow"}

        # SQL 파라미터로 전달된 merged JSON 이 올바른지 검증
        update_call = [c for c in cur.execute.call_args_list if "UPDATE users" in str(c[0][0])][0]
        params = update_call[0][1]
        merged_json = params[0]
        assert json.loads(merged_json) == {"editor_view_mode": "flow"}

    def test_merge_preserves_existing_keys(self):
        conn, cur = _conn_with_fetchone([
            {"preferences": {"theme": "dark"}},
            {"preferences": {"theme": "dark", "editor_view_mode": "block"}},
        ])
        result = users_repository.update_preferences(
            conn, USER_ID, {"editor_view_mode": "block"}
        )
        assert result == {"theme": "dark", "editor_view_mode": "block"}

        update_call = [c for c in cur.execute.call_args_list if "UPDATE users" in str(c[0][0])][0]
        merged_json = update_call[0][1][0]
        merged = json.loads(merged_json)
        assert merged == {"theme": "dark", "editor_view_mode": "block"}

    def test_null_removes_key(self):
        conn, cur = _conn_with_fetchone([
            {"preferences": {"editor_view_mode": "flow", "theme": "dark"}},
            {"preferences": {"theme": "dark"}},
        ])
        result = users_repository.update_preferences(
            conn, USER_ID, {"editor_view_mode": None}
        )
        assert "editor_view_mode" not in result
        assert result.get("theme") == "dark"

        update_call = [c for c in cur.execute.call_args_list if "UPDATE users" in str(c[0][0])][0]
        merged_json = update_call[0][1][0]
        merged = json.loads(merged_json)
        assert "editor_view_mode" not in merged
        assert merged["theme"] == "dark"

    def test_returning_row_none_returns_merged(self):
        conn, _ = _conn_with_fetchone([
            {"preferences": {}},
            None,       # RETURNING row 없음 (이론상 드문 케이스)
        ])
        result = users_repository.update_preferences(
            conn, USER_ID, {"editor_view_mode": "block"}
        )
        assert result == {"editor_view_mode": "block"}

    def test_returning_json_string_is_parsed(self):
        conn, _ = _conn_with_fetchone([
            {"preferences": {}},
            {"preferences": json.dumps({"editor_view_mode": "flow"})},
        ])
        result = users_repository.update_preferences(
            conn, USER_ID, {"editor_view_mode": "flow"}
        )
        assert result == {"editor_view_mode": "flow"}

    def test_empty_patch_is_noop_on_values(self):
        conn, cur = _conn_with_fetchone([
            {"preferences": {"theme": "dark"}},
            {"preferences": {"theme": "dark"}},
        ])
        result = users_repository.update_preferences(conn, USER_ID, {})
        assert result == {"theme": "dark"}
