"""
ExtractionSchemaRepository 단위 테스트 — Phase 8 FG8.1

DB mock(MagicMock)을 사용하여 실제 DB 연결 없이 Repository 로직을 검증한다.

테스트 범위:
- create: 정상 생성, 중복 생성 시 예외
- get_by_doc_type: 조회, 없을 때 None
- get_by_doc_type_and_version: 특정 버전 조회
- get_versions: 버전 이력 목록
- update: 새 버전 생성
- delete / restore: 소프트 삭제 및 복구
- deprecate: 폐기 표시
- list_all: 전체 목록 조회
- search_by_field_name: 필드명 검색
- ActorInfo: actor_type 검증
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call
from uuid import UUID, uuid4

import pytest

from app.models.extraction import ExtractionFieldDef, ExtractionTargetSchema
from app.repositories.extraction_schema_repository import (
    ActorInfo,
    ExtractionSchemaAlreadyExistsError,
    ExtractionSchemaNotFoundError,
    ExtractionSchemaRepository,
)

_NOW = datetime.now(timezone.utc)
_SCHEMA_ID = str(uuid4())
_DOC_TYPE = "POLICY"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def repo(mock_conn):
    return ExtractionSchemaRepository(conn=mock_conn)


@pytest.fixture
def actor_user():
    return ActorInfo(actor_id="user_001", actor_type="user")


@pytest.fixture
def actor_agent():
    return ActorInfo(actor_id="agent_007", actor_type="agent")


@pytest.fixture
def sample_fields():
    return {
        "invoice_number": ExtractionFieldDef(
            field_name="invoice_number",
            field_type="string",
            required=True,
            description="인보이스 번호",
            pattern=r"^INV-\d{6}$",
            examples=["INV-000001", "INV-999999"],
            max_length=20,
        ),
        "total_amount": ExtractionFieldDef(
            field_name="total_amount",
            field_type="number",
            required=True,
            description="총액",
            examples=["1000.50", "2500.00"],
            min_value=0.0,
            max_value=999999.99,
        ),
    }


def _make_schema_row(
    doc_type_code: str = _DOC_TYPE,
    version: int = 1,
    is_deprecated: bool = False,
) -> dict:
    fields = {
        "invoice_number": {
            "field_name": "invoice_number",
            "field_type": "string",
            "required": True,
            "description": "인보이스 번호",
            "pattern": None,
            "instruction": None,
            "examples": ["INV-001", "INV-002"],
            "max_length": None,
            "min_value": None,
            "max_value": None,
            "date_format": None,
            "enum_values": None,
            "default_value": None,
            "nested_schema": None,
        }
    }
    return {
        "id": _SCHEMA_ID,
        "doc_type_code": doc_type_code,
        "version": version,
        "fields_json": fields,
        "extra_metadata": {},
        "is_deprecated": is_deprecated,
        "deprecation_reason": "폐기 사유" if is_deprecated else None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "created_by": "user_001",
        "updated_by": "user_001",
        "scope_profile_id": None,
    }


# ---------------------------------------------------------------------------
# TestActorInfo
# ---------------------------------------------------------------------------

class TestActorInfo:
    def test_user_type(self):
        a = ActorInfo(actor_id="u001", actor_type="user")
        assert a.actor_type == "user"

    def test_agent_type(self):
        a = ActorInfo(actor_id="a001", actor_type="agent")
        assert a.actor_type == "agent"

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="actor_type"):
            ActorInfo(actor_id="x001", actor_type="robot")

    def test_default_type_is_user(self):
        a = ActorInfo(actor_id="u001")
        assert a.actor_type == "user"


# ---------------------------------------------------------------------------
# TestCreate
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_success(self, repo, mock_conn, sample_fields, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, _make_schema_row()]  # 중복 없음 → RETURNING 행

        result = repo.create(
            doc_type_code=_DOC_TYPE,
            fields=sample_fields,
            actor_info=actor_user,
        )

        assert result.doc_type_code == _DOC_TYPE
        assert result.version == 1
        assert result.created_by == "user_001"

    def test_create_duplicate_raises(self, repo, mock_conn, sample_fields, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"id": _SCHEMA_ID}  # 이미 존재

        with pytest.raises(ExtractionSchemaAlreadyExistsError):
            repo.create(
                doc_type_code=_DOC_TYPE,
                fields=sample_fields,
                actor_info=actor_user,
            )

    def test_create_with_scope_profile(self, repo, mock_conn, sample_fields, actor_agent):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        sp_id = uuid4()
        row = _make_schema_row()
        row["scope_profile_id"] = str(sp_id)
        cur.fetchone.side_effect = [None, row]

        result = repo.create(
            doc_type_code=_DOC_TYPE,
            fields=sample_fields,
            actor_info=actor_agent,
            scope_profile_id=sp_id,
        )

        assert result.scope_profile_id == sp_id

    def test_create_records_actor_id(self, repo, mock_conn, sample_fields, actor_agent):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        row = _make_schema_row()
        row["created_by"] = "agent_007"
        row["updated_by"] = "agent_007"
        cur.fetchone.side_effect = [None, row]

        result = repo.create(
            doc_type_code=_DOC_TYPE,
            fields=sample_fields,
            actor_info=actor_agent,
        )

        assert result.created_by == "agent_007"


# ---------------------------------------------------------------------------
# TestGetByDocType
# ---------------------------------------------------------------------------

class TestGetByDocType:
    def test_returns_schema_when_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = _make_schema_row()

        result = repo.get_by_doc_type(_DOC_TYPE)

        assert result is not None
        assert result.doc_type_code == _DOC_TYPE

    def test_returns_none_when_not_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = repo.get_by_doc_type("NON_EXISTENT")

        assert result is None

    def test_returns_latest_version(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = _make_schema_row(version=5)

        result = repo.get_by_doc_type(_DOC_TYPE)

        assert result.version == 5


# ---------------------------------------------------------------------------
# TestGetByDocTypeAndVersion
# ---------------------------------------------------------------------------

class TestGetByDocTypeAndVersion:
    def test_specific_version_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = _make_schema_row(version=3)

        result = repo.get_by_doc_type_and_version(_DOC_TYPE, 3)

        assert result is not None
        assert result.version == 3

    def test_specific_version_not_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = repo.get_by_doc_type_and_version(_DOC_TYPE, 99)

        assert result is None


# ---------------------------------------------------------------------------
# TestGetVersions
# ---------------------------------------------------------------------------

class TestGetVersions:
    def test_returns_version_history(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"id": _SCHEMA_ID}
        cur.fetchall.return_value = [
            {
                "id": str(uuid4()), "schema_id": _SCHEMA_ID,
                "version": 2, "fields_json": {},
                "extra_metadata": {}, "is_deprecated": False,
                "deprecation_reason": None, "change_summary": "업데이트",
                "changed_fields": [], "created_at": _NOW, "created_by": "user_001",
            },
            {
                "id": str(uuid4()), "schema_id": _SCHEMA_ID,
                "version": 1, "fields_json": {},
                "extra_metadata": {}, "is_deprecated": False,
                "deprecation_reason": None, "change_summary": "초기 생성",
                "changed_fields": [], "created_at": _NOW, "created_by": "user_001",
            },
        ]

        result = repo.get_versions(_DOC_TYPE)

        assert len(result) == 2
        assert result[0].version == 2

    def test_returns_empty_when_schema_not_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = repo.get_versions("NO_TYPE")

        assert result == []


# ---------------------------------------------------------------------------
# TestUpdate
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_increments_version(self, repo, mock_conn, sample_fields, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 1, "fields_json": {"invoice_number": {}}},
            _make_schema_row(version=2),
        ]

        result = repo.update(
            _DOC_TYPE,
            fields=sample_fields,
            actor_info=actor_user,
            change_summary="필드 추가",
        )

        assert result.version == 2

    def test_update_not_found_raises(self, repo, mock_conn, sample_fields, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        with pytest.raises(ExtractionSchemaNotFoundError):
            repo.update("NO_TYPE", fields=sample_fields, actor_info=actor_user)


# ---------------------------------------------------------------------------
# TestDelete / Restore
# ---------------------------------------------------------------------------

class TestDeleteRestore:
    def test_delete_returns_true_when_found(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1

        result = repo.delete(_DOC_TYPE, actor_user)

        assert result is True

    def test_delete_returns_false_when_not_found(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 0

        result = repo.delete("NO_TYPE", actor_user)

        assert result is False

    def test_restore_returns_true_when_found(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1

        result = repo.restore(_DOC_TYPE, actor_user)

        assert result is True

    def test_restore_returns_false_when_not_found(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 0

        result = repo.restore("NO_TYPE", actor_user)

        assert result is False


# ---------------------------------------------------------------------------
# TestDeprecate
# ---------------------------------------------------------------------------

class TestDeprecate:
    def test_deprecate_success(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = _make_schema_row(is_deprecated=True)

        result = repo.deprecate(_DOC_TYPE, reason="새 스키마로 대체", actor_info=actor_user)

        assert result.is_deprecated is True
        assert result.deprecation_reason is not None

    def test_deprecate_not_found_raises(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        with pytest.raises(ExtractionSchemaNotFoundError):
            repo.deprecate("NO_TYPE", reason="이유", actor_info=actor_user)


# ---------------------------------------------------------------------------
# TestListAll
# ---------------------------------------------------------------------------

class TestListAll:
    def test_list_all_returns_schemas(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            _make_schema_row(doc_type_code="POLICY"),
            _make_schema_row(doc_type_code="MANUAL"),
        ]

        result = repo.list_all()

        assert len(result) == 2

    def test_list_all_empty(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []

        result = repo.list_all()

        assert result == []

    def test_list_all_deprecated_filter(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [_make_schema_row(is_deprecated=True)]

        result = repo.list_all(is_deprecated=True)

        assert all(s.is_deprecated for s in result)


# ---------------------------------------------------------------------------
# TestSearchByFieldName
# ---------------------------------------------------------------------------

class TestSearchByFieldName:
    def test_search_returns_matching_schemas(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [_make_schema_row()]

        result = repo.search_by_field_name("invoice_number")

        assert len(result) == 1

    def test_search_returns_empty_when_no_match(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []

        result = repo.search_by_field_name("nonexistent_field")

        assert result == []


# ---------------------------------------------------------------------------
# TestGetVersion (P4-A) — 개별 버전 조회
# ---------------------------------------------------------------------------


def _make_version_row(version: int = 1, fields_json: dict | None = None) -> dict:
    return {
        "id": str(uuid4()),
        "schema_id": _SCHEMA_ID,
        "version": version,
        "fields_json": fields_json or {"x": {"field_name": "x", "field_type": "string",
                                              "description": "desc", "required": True}},
        "extra_metadata": {},
        "is_deprecated": False,
        "deprecation_reason": None,
        "change_summary": None,
        "changed_fields": [],
        "created_at": _NOW,
        "created_by": "user_001",
    }


class TestGetVersion:
    def test_returns_version_when_found(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID},                  # schema lookup
            _make_version_row(version=3),        # version row
        ]

        result = repo.get_version(_DOC_TYPE, 3)

        assert result is not None
        assert result.version == 3

    def test_returns_none_when_schema_missing(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        result = repo.get_version(_DOC_TYPE, 1)

        assert result is None

    def test_returns_none_when_version_missing(self, repo, mock_conn):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID},
            None,
        ]

        result = repo.get_version(_DOC_TYPE, 99)

        assert result is None

    def test_scope_profile_id_is_part_of_schema_lookup(self, repo, mock_conn):
        """scope_profile_id 가 전달되면 schema_id 조회 WHERE 절에 포함돼야 한다 (S2 ⑥)."""
        cur = mock_conn.cursor.return_value.__enter__.return_value
        sp_id = uuid4()
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID},
            _make_version_row(version=1),
        ]

        repo.get_version(_DOC_TYPE, 1, scope_profile_id=sp_id)

        # 첫 번째 execute 는 schema_id 조회. SQL 에 scope_profile_id 조건이 포함돼야 함.
        first_call_args = cur.execute.call_args_list[0]
        sql = first_call_args[0][0]
        params = first_call_args[0][1]
        assert "scope_profile_id" in sql
        assert str(sp_id) in params


# ---------------------------------------------------------------------------
# TestRollbackToVersion (P4-B) — 과거 버전으로 새 버전 생성
# ---------------------------------------------------------------------------


class TestRollbackToVersion:
    def test_rolls_back_happy_path(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        target_fields = {
            "a": {"field_name": "a", "field_type": "string",
                  "description": "desc", "required": True},
        }
        # 1) FOR UPDATE on extraction_schemas → 현재 v2
        # 2) target version fields fetch
        # 3) UPDATE RETURNING new row v3
        # 4) prev (current) version fields fetch (for changed_fields diff)
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 2, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=3),
            {"fields_json": {"a": {}, "removed_in_current": {}}},
        ]

        result = repo.rollback_to_version(
            _DOC_TYPE,
            target_version=1,
            actor_info=actor_user,
        )

        assert result.version == 3
        # INSERT INTO extraction_schema_versions 가 실행돼야 한다 (immutable 히스토리 증적).
        executed_sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert any("INSERT INTO extraction_schema_versions" in s for s in executed_sqls)

    def test_raises_not_found_when_schema_missing(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None

        with pytest.raises(ExtractionSchemaNotFoundError):
            repo.rollback_to_version(
                "NO_TYPE",
                target_version=1,
                actor_info=actor_user,
            )

    def test_rejects_when_schema_deprecated(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {
            "id": _SCHEMA_ID, "version": 3, "is_deprecated": True
        }

        with pytest.raises(ValueError, match="폐기"):
            repo.rollback_to_version(
                _DOC_TYPE,
                target_version=1,
                actor_info=actor_user,
            )

    def test_rejects_when_target_equals_current(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {
            "id": _SCHEMA_ID, "version": 2, "is_deprecated": False
        }

        with pytest.raises(ValueError, match="target_version"):
            repo.rollback_to_version(
                _DOC_TYPE,
                target_version=2,
                actor_info=actor_user,
            )

    def test_rejects_when_target_greater_than_current(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {
            "id": _SCHEMA_ID, "version": 2, "is_deprecated": False
        }

        with pytest.raises(ValueError, match="target_version"):
            repo.rollback_to_version(
                _DOC_TYPE,
                target_version=5,
                actor_info=actor_user,
            )

    def test_rejects_when_target_version_row_missing(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 3, "is_deprecated": False},
            None,  # target_row fetch
        ]

        with pytest.raises(ExtractionSchemaNotFoundError, match="target_version"):
            repo.rollback_to_version(
                _DOC_TYPE,
                target_version=1,
                actor_info=actor_user,
            )

    def test_rejects_when_target_fields_empty(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 3, "is_deprecated": False},
            {"fields_json": {}},  # 빈 fields → 거절
        ]

        with pytest.raises(ValueError, match="비어"):
            repo.rollback_to_version(
                _DOC_TYPE,
                target_version=1,
                actor_info=actor_user,
            )

    def test_default_change_summary_is_generated(self, repo, mock_conn, actor_user):
        """change_summary 를 비워두면 'v{N} 로 되돌리기' 가 기록돼야 한다."""
        cur = mock_conn.cursor.return_value.__enter__.return_value
        target_fields = {"a": {"field_name": "a"}}
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 2, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=3),
            {"fields_json": target_fields},
        ]

        repo.rollback_to_version(
            _DOC_TYPE,
            target_version=1,
            actor_info=actor_user,
            change_summary=None,
        )

        # 버전 INSERT 의 parameters 에 'v1 로 되돌리기' 문자열이 포함돼야 함
        insert_call = next(
            c for c in cur.execute.call_args_list
            if "INSERT INTO extraction_schema_versions" in c[0][0]
        )
        params = insert_call[0][1]
        assert any("v1 로 되돌리기" == p for p in params)

    def test_custom_change_summary_preserved(self, repo, mock_conn, actor_user):
        cur = mock_conn.cursor.return_value.__enter__.return_value
        target_fields = {"a": {"field_name": "a"}}
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 2, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=3),
            {"fields_json": target_fields},
        ]

        repo.rollback_to_version(
            _DOC_TYPE,
            target_version=1,
            actor_info=actor_user,
            change_summary="legal 승인",
        )

        insert_call = next(
            c for c in cur.execute.call_args_list
            if "INSERT INTO extraction_schema_versions" in c[0][0]
        )
        params = insert_call[0][1]
        assert "legal 승인" in params

    def test_rollback_records_rolled_back_from_metadata(self, repo, mock_conn, actor_user):
        """extra_metadata 에 rolled_back_from_version 이 기록돼야 함 (감사 추적)."""
        cur = mock_conn.cursor.return_value.__enter__.return_value
        target_fields = {"a": {"field_name": "a"}}
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 5, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=6),
            {"fields_json": target_fields},
        ]

        repo.rollback_to_version(
            _DOC_TYPE,
            target_version=3,
            actor_info=actor_user,
        )

        insert_call = next(
            c for c in cur.execute.call_args_list
            if "INSERT INTO extraction_schema_versions" in c[0][0]
        )
        params = insert_call[0][1]
        meta_json = next(p for p in params if isinstance(p, str)
                         and "rolled_back_from_version" in p)
        assert '"rolled_back_from_version": 3' in meta_json

    def test_rollback_uses_for_update_lock(self, repo, mock_conn, actor_user):
        """경쟁 조건 방지를 위해 SELECT ... FOR UPDATE 를 사용해야 한다."""
        cur = mock_conn.cursor.return_value.__enter__.return_value
        target_fields = {"a": {"field_name": "a"}}
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 2, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=3),
            {"fields_json": target_fields},
        ]

        repo.rollback_to_version(
            _DOC_TYPE,
            target_version=1,
            actor_info=actor_user,
        )

        first_sql = cur.execute.call_args_list[0][0][0]
        assert "FOR UPDATE" in first_sql

    def test_scope_profile_guard_in_lookup(self, repo, mock_conn, actor_user):
        """scope_profile_id 지정 시 첫 번째 조회 SQL 에 조건이 포함돼야 함 (S2 ⑥)."""
        cur = mock_conn.cursor.return_value.__enter__.return_value
        sp_id = uuid4()
        target_fields = {"a": {"field_name": "a"}}
        cur.fetchone.side_effect = [
            {"id": _SCHEMA_ID, "version": 2, "is_deprecated": False},
            {"fields_json": target_fields},
            _make_schema_row(version=3),
            {"fields_json": target_fields},
        ]

        repo.rollback_to_version(
            _DOC_TYPE,
            target_version=1,
            actor_info=actor_user,
            scope_profile_id=sp_id,
        )

        first_call = cur.execute.call_args_list[0]
        sql = first_call[0][0]
        params = first_call[0][1]
        assert "scope_profile_id" in sql
        assert str(sp_id) in params
