"""FG 0-3 S16: 리포 게이트 보강 (settings_repository + nodes_repository + 기타).

repositories 74.83% → 80%+ 로 끌어올리기 위한 작은 리포 묶음 테스트.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.settings_repository import SettingsRepository, _row_to_setting
from app.repositories.nodes_repository import NodesRepository, _row_to_node


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _mk_cur(fetchone_values=None, fetchall_values=None, rowcount=0):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    if fetchone_values is not None:
        cur.fetchone = MagicMock(side_effect=list(fetchone_values))
    else:
        cur.fetchone = MagicMock(return_value=None)
    if fetchall_values is not None:
        cur.fetchall = MagicMock(side_effect=list(fetchall_values))
    else:
        cur.fetchall = MagicMock(return_value=[])
    cur.rowcount = rowcount
    return cur


def _mk_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


# ---------------------------------------------------------------------------
# settings_repository
# ---------------------------------------------------------------------------


def _mk_setting_row(
    id_="11111111-1111-1111-1111-111111111111",
    category="system",
    key="retention_days",
    value=90,
    updated_by="user-1",
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "category": category,
        "key": key,
        "value": value,
        "description": "설명",
        "updated_by": updated_by,
        "updated_at": now,
    }


def test_row_to_setting_happy():
    row = _mk_setting_row()
    result = _row_to_setting(row)
    assert result["category"] == "system"
    assert result["value"] == 90
    assert isinstance(result["id"], str)


def test_row_to_setting_null_updated_by():
    row = _mk_setting_row(updated_by=None)
    row["updated_by"] = None
    result = _row_to_setting(row)
    assert result["updated_by"] is None


def test_settings_list_all():
    cur = _mk_cur(fetchall_values=[[_mk_setting_row(), _mk_setting_row(key="other")]])
    repo = SettingsRepository()
    result = repo.list_all(_mk_conn(cur))
    assert len(result) == 2
    sql = cur.execute.call_args[0][0]
    assert "ORDER BY category, key" in sql


def test_settings_list_by_category():
    cur = _mk_cur(fetchall_values=[[_mk_setting_row()]])
    repo = SettingsRepository()
    result = repo.list_by_category(_mk_conn(cur), "system")
    assert len(result) == 1
    params = cur.execute.call_args[0][1]
    assert params == ("system",)


def test_settings_get_one_found():
    cur = _mk_cur(fetchone_values=[_mk_setting_row()])
    repo = SettingsRepository()
    result = repo.get_one(_mk_conn(cur), "system", "retention_days")
    assert result is not None
    assert result["key"] == "retention_days"


def test_settings_get_one_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = SettingsRepository()
    assert repo.get_one(_mk_conn(cur), "system", "missing") is None


def test_settings_list_categories():
    cur = _mk_cur(fetchall_values=[[
        {"category": "system"},
        {"category": "rag"},
    ]])
    repo = SettingsRepository()
    result = repo.list_categories(_mk_conn(cur))
    assert result == ["system", "rag"]


def test_settings_update_value_success():
    cur = _mk_cur(fetchone_values=[_mk_setting_row(value=60)])
    repo = SettingsRepository()
    result = repo.update_value(_mk_conn(cur), "system", "retention_days", 60, "admin-1")
    assert result is not None
    assert result["value"] == 60
    params = cur.execute.call_args[0][1]
    # JSON 직렬화된 값 포함
    assert "60" in params[0]
    assert params[1] == "admin-1"


def test_settings_update_value_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = SettingsRepository()
    result = repo.update_value(_mk_conn(cur), "system", "missing", 60, "admin-1")
    assert result is None


def test_settings_update_value_serializes_complex_json():
    cur = _mk_cur(fetchone_values=[_mk_setting_row(value={"nested": True})])
    repo = SettingsRepository()
    repo.update_value(_mk_conn(cur), "system", "complex", {"nested": True}, None)
    params = cur.execute.call_args[0][1]
    # JSON 직렬화된 dict
    assert "nested" in params[0]


# ---------------------------------------------------------------------------
# nodes_repository
# ---------------------------------------------------------------------------


def _mk_node_row(
    id_="11111111-1111-1111-1111-111111111111",
    version_id="22222222-2222-2222-2222-222222222222",
    parent_id=None,
    metadata=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "version_id": version_id,
        "parent_id": parent_id,
        "node_type": "section",
        "order_index": 0,
        "title": "제목",
        "content": "본문",
        "metadata": metadata if metadata is not None else {},
        "created_at": now,
    }


def test_row_to_node_happy():
    node = _row_to_node(_mk_node_row())
    assert node.node_type == "section"
    assert node.title == "제목"
    assert node.parent_id is None


def test_row_to_node_with_parent():
    row = _mk_node_row(parent_id="pp")
    node = _row_to_node(row)
    assert node.parent_id == "pp"


def test_row_to_node_null_metadata_defaults_to_empty_dict():
    row = _mk_node_row()
    row["metadata"] = None
    node = _row_to_node(row)
    assert node.metadata == {}


def test_nodes_bulk_create_empty_returns_empty():
    repo = NodesRepository()
    assert repo.bulk_create_for_version(MagicMock(), "v1", []) == []


def test_nodes_bulk_create_inserts_each():
    cur = _mk_cur(fetchone_values=[
        _mk_node_row(id_="n1"), _mk_node_row(id_="n2")
    ])
    repo = NodesRepository()
    result = repo.bulk_create_for_version(
        _mk_conn(cur),
        "v1",
        [
            {"node_type": "section", "order_index": 0, "title": "T1", "content": "C1"},
            {"node_type": "paragraph", "order_index": 1, "title": "T2", "content": "C2"},
        ],
    )
    assert len(result) == 2
    # execute 2회
    assert cur.execute.call_count == 2


def test_nodes_bulk_create_uses_defaults():
    cur = _mk_cur(fetchone_values=[_mk_node_row()])
    repo = NodesRepository()
    repo.bulk_create_for_version(
        _mk_conn(cur), "v1", [{}]  # 모든 키 누락
    )
    params = cur.execute.call_args[0][1]
    # node_type 기본값 "paragraph" 포함
    assert "paragraph" in params


def test_nodes_list_by_version_id():
    cur = _mk_cur(fetchall_values=[[_mk_node_row(), _mk_node_row()]])
    repo = NodesRepository()
    result = repo.list_by_version_id(_mk_conn(cur), "v1")
    assert len(result) == 2
    sql = cur.execute.call_args[0][0]
    assert "ORDER BY order_index ASC" in sql


def test_nodes_replace_empty_items_after_delete():
    cur = _mk_cur()
    repo = NodesRepository()
    result = repo.replace_for_version(_mk_conn(cur), "v1", [])
    assert result == []
    # DELETE 만 호출
    sql = cur.execute.call_args[0][0]
    assert "DELETE FROM nodes" in sql


def test_nodes_replace_with_valid_uuid_id_preserves():
    valid_uuid = "33333333-3333-3333-3333-333333333333"
    cur = _mk_cur(fetchone_values=[_mk_node_row(id_=valid_uuid)])
    repo = NodesRepository()
    result = repo.replace_for_version(
        _mk_conn(cur),
        "v1",
        [{"id": valid_uuid, "node_type": "section", "order_index": 0}],
    )
    assert len(result) == 1


def test_nodes_replace_with_invalid_uuid_generates_new():
    cur = _mk_cur(fetchone_values=[_mk_node_row()])
    repo = NodesRepository()
    result = repo.replace_for_version(
        _mk_conn(cur),
        "v1",
        [{"id": "invalid-uuid", "node_type": "section"}],
    )
    assert len(result) == 1
    # INSERT 호출의 첫 파라미터가 새 UUID (원본 "invalid-uuid" 가 아님)
    insert_call = [c for c in cur.execute.call_args_list if "INSERT" in c[0][0]][0]
    node_id_param = insert_call[0][1][0]
    assert node_id_param != "invalid-uuid"
    assert len(node_id_param) == 36  # UUID 형식


def test_nodes_replace_with_no_id_generates_new():
    cur = _mk_cur(fetchone_values=[_mk_node_row()])
    repo = NodesRepository()
    repo.replace_for_version(
        _mk_conn(cur), "v1", [{"node_type": "section"}]
    )
    insert_call = [c for c in cur.execute.call_args_list if "INSERT" in c[0][0]][0]
    node_id_param = insert_call[0][1][0]
    assert len(node_id_param) == 36  # UUID 자동 생성됨


def test_nodes_get_by_id_and_version_found():
    cur = _mk_cur(fetchone_values=[_mk_node_row()])
    repo = NodesRepository()
    node = repo.get_by_id_and_version_id(_mk_conn(cur), "n1", "v1")
    assert node is not None
    params = cur.execute.call_args[0][1]
    assert params == ("n1", "v1")


def test_nodes_get_by_id_and_version_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = NodesRepository()
    assert repo.get_by_id_and_version_id(_mk_conn(cur), "n1", "v1") is None
