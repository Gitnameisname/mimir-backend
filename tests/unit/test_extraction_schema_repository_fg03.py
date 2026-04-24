"""FG 0-3 커버리지 보강 — extraction_schema_repository 유닛 테스트 (세션 12-A).

대상: `backend/app/repositories/extraction_schema_repository.py` (701줄)

커버 범위:
  - ActorInfo 초기화 + 유효성 검사
  - _fields_to_json / _json_to_fields 라운드트립 (None/dict/str)
  - _row_to_schema + _row_to_version
  - create (성공 / 중복 → AlreadyExistsError)
  - get_by_doc_type (found/None/include_deprecated/scope 필터)
  - get_by_doc_type_and_version (found/None)
  - get_version (schema 없음/version 없음/scope 필터)
  - get_versions (schema 없음/rows 있음/scope)
  - list_all (기본/is_deprecated/scope/include_deleted)
  - search_by_field_name (기본/scope/include_deleted)
  - update (성공 / NotFoundError)
  - rollback_to_version (성공/스키마 없음/deprecated/invalid target/target 이력 없음/빈 fields/change_summary 없음 기본값/scope)
  - delete + restore (rowcount True/False)
  - deprecate (성공/NotFoundError)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.repositories.extraction_schema_repository import (
    ActorInfo,
    ExtractionSchemaAlreadyExistsError,
    ExtractionSchemaNotFoundError,
    ExtractionSchemaRepository,
)


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


def _mk_actor(actor_id="user-1", actor_type="user"):
    return ActorInfo(actor_id, actor_type)


_DEFAULT_FIELD_JSON = {
    "title": {
        "field_name": "title",
        "field_type": "string",
        "required": True,
        "description": "제목 필드",
    }
}


def _mk_schema_row(
    id_="11111111-1111-1111-1111-111111111111",
    doc_type="REPORT",
    version=1,
    fields_json=None,
    is_deprecated=False,
    scope_profile_id=None,
    extra_metadata=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "doc_type_code": doc_type,
        "version": version,
        "fields_json": fields_json if fields_json is not None else _DEFAULT_FIELD_JSON,
        "extra_metadata": extra_metadata or {},
        "is_deprecated": is_deprecated,
        "deprecation_reason": "폐기 사유" if is_deprecated else None,
        "created_at": now,
        "updated_at": now,
        "created_by": "user-1",
        "updated_by": "user-1",
        "scope_profile_id": scope_profile_id,
    }


def _mk_version_row(
    id_="22222222-2222-2222-2222-222222222222",
    schema_id="11111111-1111-1111-1111-111111111111",
    version=1,
    fields_json=None,
    changed_fields=None,
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "schema_id": schema_id,
        "version": version,
        "fields_json": fields_json if fields_json is not None else _DEFAULT_FIELD_JSON,
        "extra_metadata": {},
        "is_deprecated": False,
        "deprecation_reason": None,
        "change_summary": "초기 생성",
        "changed_fields": changed_fields or [],
        "created_at": now,
        "created_by": "user-1",
    }


# ---------------------------------------------------------------------------
# 1. ActorInfo
# ---------------------------------------------------------------------------


def test_actor_info_accepts_user_type():
    a = ActorInfo("u1", "user")
    assert a.actor_id == "u1"
    assert a.actor_type == "user"


def test_actor_info_accepts_agent_type():
    a = ActorInfo("bot-1", "agent")
    assert a.actor_type == "agent"


def test_actor_info_defaults_to_user():
    a = ActorInfo("u1")
    assert a.actor_type == "user"


def test_actor_info_invalid_type_raises():
    with pytest.raises(ValueError):
        ActorInfo("u1", "admin")


# ---------------------------------------------------------------------------
# 2. Internal helpers (JSON round-trip, row mapping)
# ---------------------------------------------------------------------------


def test_fields_to_json_and_back_roundtrip():
    """ExtractionFieldDef 기반 딕셔너리 → JSON → 딕셔너리 라운드트립 검증."""
    from app.models.extraction import ExtractionFieldDef
    repo = ExtractionSchemaRepository(MagicMock())
    field = ExtractionFieldDef(field_name="title", field_type="string", required=True, description="제목 필드")
    js = repo._fields_to_json({"title": field})
    back = repo._json_to_fields(js)
    assert "title" in back
    assert back["title"].field_name == "title"


def test_json_to_fields_accepts_dict_input():
    """DB 에서 dict 로 이미 디코딩된 경우도 허용."""
    repo = ExtractionSchemaRepository(MagicMock())
    raw = {"title": {"field_name": "title", "field_type": "string", "required": True, "description": "제목"}}
    result = repo._json_to_fields(raw)
    assert "title" in result


def test_json_to_fields_none_returns_empty():
    repo = ExtractionSchemaRepository(MagicMock())
    assert repo._json_to_fields(None) == {}


def test_row_to_schema_happy_path():
    repo = ExtractionSchemaRepository(MagicMock())
    row = _mk_schema_row(
        scope_profile_id="99999999-9999-9999-9999-999999999999"
    )
    schema = repo._row_to_schema(row)
    assert schema.doc_type_code == "REPORT"
    assert schema.version == 1
    assert schema.scope_profile_id is not None


def test_row_to_schema_without_scope_and_metadata():
    repo = ExtractionSchemaRepository(MagicMock())
    row = _mk_schema_row()
    # 명시적으로 None 처리
    row["scope_profile_id"] = None
    row["extra_metadata"] = None
    schema = repo._row_to_schema(row)
    assert schema.scope_profile_id is None
    assert schema.extra_metadata == {}


def test_row_to_version_happy_path():
    repo = ExtractionSchemaRepository(MagicMock())
    row = _mk_version_row(changed_fields=["title"])
    ver = repo._row_to_version(row)
    assert ver.version == 1
    assert ver.changed_fields == ["title"]


def test_row_to_version_default_fields_when_none():
    repo = ExtractionSchemaRepository(MagicMock())
    row = _mk_version_row()
    # changed_fields / extra_metadata 가 None 인 경우 빈 리스트/딕셔너리로 기본값
    row["changed_fields"] = None
    row["extra_metadata"] = None
    ver = repo._row_to_version(row)
    assert ver.changed_fields == []
    assert ver.extra_metadata == {}


# ---------------------------------------------------------------------------
# 3. create
# ---------------------------------------------------------------------------


def test_create_success():
    from app.models.extraction import ExtractionFieldDef
    cur = _mk_cur(
        fetchone_values=[
            None,  # 중복 체크 — 없음
            _mk_schema_row(),  # INSERT RETURNING
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    schema = repo.create(
        doc_type_code="REPORT",
        fields={"title": ExtractionFieldDef(field_name="title", field_type="string", required=True, description="제목 필드")},
        actor_info=_mk_actor(),
    )
    assert schema.doc_type_code == "REPORT"
    # INSERT 가 2개 (schemas + versions)
    assert cur.execute.call_count == 3  # 중복 체크 + schemas + versions


def test_create_duplicate_raises():
    from app.models.extraction import ExtractionFieldDef
    cur = _mk_cur(
        fetchone_values=[{"id": "existing"}]  # 중복 존재
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ExtractionSchemaAlreadyExistsError):
        repo.create(
            doc_type_code="REPORT",
            fields={"title": ExtractionFieldDef(field_name="title", field_type="string", required=True, description="제목 필드")},
            actor_info=_mk_actor(),
        )


def test_create_with_scope_and_metadata():
    from app.models.extraction import ExtractionFieldDef
    cur = _mk_cur(
        fetchone_values=[
            None,  # 중복 체크 — 없음
            _mk_schema_row(scope_profile_id=str(uuid4())),
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.create(
        doc_type_code="REPORT",
        fields={"title": ExtractionFieldDef(field_name="title", field_type="string", required=True, description="제목 필드")},
        actor_info=_mk_actor(),
        scope_profile_id=sid,
        extra_metadata={"source": "manual"},
    )
    # 파라미터에 scope_profile_id 문자열 포함 확인 (insert 호출이 2번째)
    insert_args = cur.execute.call_args_list[1]
    assert str(sid) in insert_args[0][1]


# ---------------------------------------------------------------------------
# 4. get_by_doc_type
# ---------------------------------------------------------------------------


def test_get_by_doc_type_found():
    cur = _mk_cur(fetchone_values=[_mk_schema_row()])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.get_by_doc_type("REPORT")
    assert result is not None
    assert result.doc_type_code == "REPORT"


def test_get_by_doc_type_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.get_by_doc_type("REPORT") is None


def test_get_by_doc_type_include_deprecated_removes_filter():
    cur = _mk_cur(fetchone_values=[_mk_schema_row(is_deprecated=True)])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    repo.get_by_doc_type("REPORT", include_deprecated=True)
    # WHERE 절에 is_deprecated 필터가 없어야 함
    sql = cur.execute.call_args[0][0]
    assert "is_deprecated = FALSE" not in sql


def test_get_by_doc_type_with_scope_filter():
    cur = _mk_cur(fetchone_values=[_mk_schema_row()])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.get_by_doc_type("REPORT", scope_profile_id=sid)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "scope_profile_id = %s" in sql
    assert str(sid) in params


# ---------------------------------------------------------------------------
# 5. get_by_doc_type_and_version
# ---------------------------------------------------------------------------


def test_get_by_doc_type_and_version_found():
    cur = _mk_cur(fetchone_values=[_mk_schema_row(version=3)])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.get_by_doc_type_and_version("REPORT", 3)
    assert result is not None
    assert result.version == 3


def test_get_by_doc_type_and_version_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.get_by_doc_type_and_version("REPORT", 99) is None


# ---------------------------------------------------------------------------
# 6. get_version
# ---------------------------------------------------------------------------


def test_get_version_schema_not_found_returns_none():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.get_version("REPORT", 1) is None


def test_get_version_version_not_found_returns_none():
    cur = _mk_cur(
        fetchone_values=[
            {"id": "11111111-1111-1111-1111-111111111111"},  # schema row
            None,  # version row 없음
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.get_version("REPORT", 1) is None


def test_get_version_found():
    cur = _mk_cur(
        fetchone_values=[
            {"id": "11111111-1111-1111-1111-111111111111"},
            _mk_version_row(version=2),
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    ver = repo.get_version("REPORT", 2)
    assert ver is not None
    assert ver.version == 2


def test_get_version_with_scope_passes_scope_param():
    cur = _mk_cur(
        fetchone_values=[
            {"id": "11111111-1111-1111-1111-111111111111"},
            _mk_version_row(),
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.get_version("REPORT", 1, scope_profile_id=sid)
    first_call_params = cur.execute.call_args_list[0][0][1]
    assert str(sid) in first_call_params


# ---------------------------------------------------------------------------
# 7. get_versions
# ---------------------------------------------------------------------------


def test_get_versions_schema_not_found_returns_empty():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.get_versions("REPORT") == []


def test_get_versions_returns_rows_desc():
    cur = _mk_cur(
        fetchone_values=[{"id": "11111111-1111-1111-1111-111111111111"}],
        fetchall_values=[[_mk_version_row(version=2), _mk_version_row(version=1)]],
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    versions = repo.get_versions("REPORT", limit=10)
    assert len(versions) == 2
    assert versions[0].version == 2


def test_get_versions_with_scope_applies_filter():
    cur = _mk_cur(
        fetchone_values=[{"id": "11111111-1111-1111-1111-111111111111"}],
        fetchall_values=[[]],
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.get_versions("REPORT", scope_profile_id=sid)
    first_params = cur.execute.call_args_list[0][0][1]
    assert str(sid) in first_params


# ---------------------------------------------------------------------------
# 8. list_all
# ---------------------------------------------------------------------------


def test_list_all_default():
    cur = _mk_cur(fetchall_values=[[_mk_schema_row(doc_type="A"), _mk_schema_row(doc_type="B")]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.list_all()
    assert len(result) == 2
    sql = cur.execute.call_args[0][0]
    assert "is_soft_deleted = FALSE" in sql


def test_list_all_include_deleted_removes_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    repo.list_all(include_deleted=True)
    sql = cur.execute.call_args[0][0]
    assert "is_soft_deleted = FALSE" not in sql


def test_list_all_with_is_deprecated_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    repo.list_all(is_deprecated=True)
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "is_deprecated = %s" in sql
    assert True in params


def test_list_all_with_scope_filter():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.list_all(scope_profile_id=sid)
    params = cur.execute.call_args[0][1]
    assert str(sid) in params


# ---------------------------------------------------------------------------
# 9. search_by_field_name
# ---------------------------------------------------------------------------


def test_search_by_field_name_basic():
    cur = _mk_cur(fetchall_values=[[_mk_schema_row()]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.search_by_field_name("title")
    assert len(result) == 1
    sql = cur.execute.call_args[0][0]
    assert "fields_json ? %s" in sql


def test_search_by_field_name_with_scope():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    repo.search_by_field_name("title", scope_profile_id=sid)
    params = cur.execute.call_args[0][1]
    assert "title" in params
    assert str(sid) in params


def test_search_by_field_name_include_deleted():
    cur = _mk_cur(fetchall_values=[[]])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    repo.search_by_field_name("title", include_deleted=True)
    sql = cur.execute.call_args[0][0]
    assert "is_soft_deleted = FALSE" not in sql


# ---------------------------------------------------------------------------
# 10. update
# ---------------------------------------------------------------------------


def test_update_success_increments_version():
    from app.models.extraction import ExtractionFieldDef
    cur = _mk_cur(
        fetchone_values=[
            # 현재 버전 조회
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 1,
                "fields_json": {"title": {"field_name": "title", "field_type": "string", "required": True, "description": "제목"}},
            },
            # UPDATE RETURNING
            _mk_schema_row(version=2),
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.update(
        "REPORT",
        fields={"title": ExtractionFieldDef(field_name="title", field_type="string", required=True, description="제목 필드")},
        actor_info=_mk_actor(),
        change_summary="제목 업데이트",
    )
    assert result.version == 2
    assert result is not None
    # execute 3회 (select, update, insert version)
    assert cur.execute.call_count == 3


def test_update_not_found_raises():
    from app.models.extraction import ExtractionFieldDef
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ExtractionSchemaNotFoundError):
        repo.update(
            "UNKNOWN",
            fields={"t": ExtractionFieldDef(field_name="t", field_type="string", required=True, description="테스트 필드")},
            actor_info=_mk_actor(),
        )


# ---------------------------------------------------------------------------
# 11. rollback_to_version
# ---------------------------------------------------------------------------


def test_rollback_not_found_raises():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ExtractionSchemaNotFoundError):
        repo.rollback_to_version(
            "REPORT",
            target_version=1,
            actor_info=_mk_actor(),
        )


def test_rollback_deprecated_raises_value_error():
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": True,
            }
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ValueError, match="폐기된 스키마"):
        repo.rollback_to_version(
            "REPORT", target_version=1, actor_info=_mk_actor()
        )


def test_rollback_invalid_target_version_raises():
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            }
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    # target_version >= current_version
    with pytest.raises(ValueError, match="target_version"):
        repo.rollback_to_version(
            "REPORT", target_version=3, actor_info=_mk_actor()
        )
    # target_version < 1
    cur2 = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            }
        ]
    )
    repo2 = ExtractionSchemaRepository(_mk_conn(cur2))
    with pytest.raises(ValueError):
        repo2.rollback_to_version(
            "REPORT", target_version=0, actor_info=_mk_actor()
        )


def test_rollback_target_version_history_missing_raises():
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            },
            None,  # target version 이력 없음
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ExtractionSchemaNotFoundError, match="target_version"):
        repo.rollback_to_version(
            "REPORT", target_version=1, actor_info=_mk_actor()
        )


def test_rollback_empty_target_fields_raises():
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            },
            {"fields_json": {}},  # 빈 fields
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ValueError, match="비어 있어"):
        repo.rollback_to_version(
            "REPORT", target_version=1, actor_info=_mk_actor()
        )


def test_rollback_success_increments_version():
    cur = _mk_cur(
        fetchone_values=[
            # 현재 스키마
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            },
            # target version 이력
            {"fields_json": {"title": {"field_name": "title", "field_type": "string", "required": True, "description": "제목"}}},
            # UPDATE RETURNING
            _mk_schema_row(version=4),
            # 현재 최신 버전 fields (changed_fields 계산용)
            {"fields_json": {
            "title": {"field_name": "title", "field_type": "string", "required": True, "description": "제목"},
            "deprecated_field": {"field_name": "deprecated_field", "field_type": "string", "required": False, "description": "deprecated"},
        }},
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.rollback_to_version(
        "REPORT",
        target_version=1,
        actor_info=_mk_actor(),
        change_summary="v1 로 복원",
    )
    assert result.version == 4
    # execute 호출: select + select target + update + select prev + insert version history
    assert cur.execute.call_count == 5


def test_rollback_default_change_summary_when_missing():
    cur = _mk_cur(
        fetchone_values=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "version": 3,
                "is_deprecated": False,
            },
            {"fields_json": {"title": {"field_name": "title", "field_type": "string", "required": True, "description": "제목"}}},
            _mk_schema_row(version=4),
            None,  # 현재 버전 이력 없음 — prev_fields_map 빈 딕셔너리
        ]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    # change_summary 를 빈 문자열로 → 자동 기본값 "v{N} 로 되돌리기"
    repo.rollback_to_version(
        "REPORT",
        target_version=1,
        actor_info=_mk_actor(),
        change_summary="",
    )
    # INSERT history 호출 (마지막)
    insert_params = cur.execute.call_args_list[-1][0][1]
    assert any("v1 로 되돌리기" == p for p in insert_params if isinstance(p, str))


def test_rollback_with_scope_passes_param():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    sid = uuid4()
    with pytest.raises(ExtractionSchemaNotFoundError):
        repo.rollback_to_version(
            "REPORT",
            target_version=1,
            actor_info=_mk_actor(),
            scope_profile_id=sid,
        )
    first_params = cur.execute.call_args_list[0][0][1]
    assert str(sid) in first_params


# ---------------------------------------------------------------------------
# 12. delete / restore
# ---------------------------------------------------------------------------


def test_delete_returns_true_when_row_updated():
    cur = _mk_cur(rowcount=1)
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.delete("REPORT", _mk_actor()) is True


def test_delete_returns_false_when_no_row():
    cur = _mk_cur(rowcount=0)
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.delete("NOT_EXIST", _mk_actor()) is False


def test_restore_returns_true_on_update():
    cur = _mk_cur(rowcount=1)
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.restore("REPORT", _mk_actor()) is True


def test_restore_returns_false_when_no_row():
    cur = _mk_cur(rowcount=0)
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    assert repo.restore("REPORT", _mk_actor()) is False


# ---------------------------------------------------------------------------
# 13. deprecate
# ---------------------------------------------------------------------------


def test_deprecate_success():
    cur = _mk_cur(
        fetchone_values=[_mk_schema_row(is_deprecated=True)]
    )
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    result = repo.deprecate(
        "REPORT", reason="legacy schema", actor_info=_mk_actor()
    )
    assert result.is_deprecated is True


def test_deprecate_not_found_raises():
    cur = _mk_cur(fetchone_values=[None])
    repo = ExtractionSchemaRepository(_mk_conn(cur))
    with pytest.raises(ExtractionSchemaNotFoundError):
        repo.deprecate(
            "UNKNOWN", reason="bad", actor_info=_mk_actor()
        )
