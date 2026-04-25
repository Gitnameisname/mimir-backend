"""
S3 Phase 0 / FG 0-3 후속 — `app.services.documents_service` 유닛 테스트.

커버 대상 (분기):
  - _validate_metadata_against_schema: 스키마 없음 / required 통과 / required 누락
  - create_document: 정상 / 스키마 부적합
  - get_document: 정상 / 부재
  - list_documents: 정상
  - update_document: 정상 / no-op / 부재 / metadata 교체 시 스키마 재검증
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


DOC_ID = "d1111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# 헬퍼
# --------------------------------------------------------------------------- #


def _domain_doc(**kw):
    base = {
        "id": DOC_ID,
        "title": "T",
        "document_type": "policy",
        "status": "draft",
        "metadata": {"author": "X"},
        "summary": "s",
        "created_by": "author-1",
        "updated_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_draft_version_id": None,
        "current_published_version_id": None,
        # S3 Phase 2 FG 2-0 (2026-04-24): 새 필드 — 기본 None
        "scope_profile_id": None,
        # S3 Phase 2 FG 2-1 (2026-04-24): folder_id + in_collection_ids 누락 보완
        # (R-D 회귀 패치 2, 2026-04-25 — _to_response 가 doc.folder_id 접근)
        "folder_id": None,
        "in_collection_ids": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _create_req(**kw):
    from app.schemas.documents import DocumentCreateRequest
    body = {
        "title": "새 문서",
        "document_type": "policy",
        "status": "draft",
        "metadata": {"author": "작성자"},
        "summary": "요약",
    }
    body.update(kw)
    return DocumentCreateRequest(**body)


def _update_req(**kw):
    from app.schemas.documents import DocumentUpdateRequest
    return DocumentUpdateRequest(**kw)


def _make_conn_with_schema_row(row):
    """document_types.schema_fields 조회 결과를 반환하는 connection mock."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(return_value=row)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


@pytest.fixture
def mock_repo(monkeypatch):
    from app.services import documents_service as svc_mod

    repo = MagicMock()
    repo.get_by_id.return_value = _domain_doc()
    repo.create.return_value = _domain_doc(id="new-id")
    repo.update.return_value = _domain_doc(title="updated")
    repo.list.return_value = ([_domain_doc()], 1)

    monkeypatch.setattr(svc_mod, "documents_repository", repo)
    return repo


# --------------------------------------------------------------------------- #
# 1) _validate_metadata_against_schema
# --------------------------------------------------------------------------- #


class TestMetadataSchemaValidation:
    def test_skips_when_type_not_in_db(self):
        from app.services.documents_service import _validate_metadata_against_schema
        conn = _make_conn_with_schema_row(None)
        # 예외 없이 반환
        _validate_metadata_against_schema(conn, "unknown_type", {"a": 1})

    def test_skips_when_schema_empty(self):
        from app.services.documents_service import _validate_metadata_against_schema
        conn = _make_conn_with_schema_row((None,))  # schema_fields = None
        _validate_metadata_against_schema(conn, "policy", {})

    def test_passes_when_required_present(self):
        from app.services.documents_service import _validate_metadata_against_schema
        schema = [
            {"name": "author", "required": True},
            {"name": "effective_date", "required": False},
        ]
        conn = _make_conn_with_schema_row((schema,))
        _validate_metadata_against_schema(conn, "policy", {"author": "X"})

    def test_raises_when_required_missing(self):
        from app.services.documents_service import _validate_metadata_against_schema
        from app.api.errors.exceptions import ApiValidationError
        schema = [
            {"name": "author", "required": True},
            {"name": "title", "required": True},
        ]
        conn = _make_conn_with_schema_row((schema,))
        with pytest.raises(ApiValidationError, match="필수 필드"):
            _validate_metadata_against_schema(conn, "policy", {"author": "X"})  # title 누락

    def test_accepts_json_string_schema(self):
        """schema_fields 가 JSON 문자열로 저장된 케이스 — json.loads 경로."""
        from app.services.documents_service import _validate_metadata_against_schema
        import json
        schema_str = json.dumps([{"name": "author", "required": True}])
        conn = _make_conn_with_schema_row((schema_str,))
        # author 있음 → 통과
        _validate_metadata_against_schema(conn, "policy", {"author": "X"})


# --------------------------------------------------------------------------- #
# 2) create_document
# --------------------------------------------------------------------------- #


class TestCreateDocument:
    def test_happy_path_no_schema(self, mock_repo):
        from app.services.documents_service import documents_service
        conn = _make_conn_with_schema_row(None)  # 스키마 없음 → 검증 skip

        result = documents_service.create_document(
            conn, _create_req(), actor_id="author-1",
        )
        assert result.id == "new-id"
        assert mock_repo.create.called
        call_kwargs = mock_repo.create.call_args.kwargs
        assert call_kwargs["created_by"] == "author-1"
        assert call_kwargs["document_type"] == "policy"

    def test_metadata_validation_failure_blocks_create(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.api.errors.exceptions import ApiValidationError

        schema = [{"name": "author", "required": True}]
        conn = _make_conn_with_schema_row((schema,))

        req = _create_req(metadata={})  # author 누락
        with pytest.raises(ApiValidationError):
            documents_service.create_document(conn, req, actor_id="a")
        # 검증 실패 시 repo.create 는 호출되지 않아야 함
        assert not mock_repo.create.called


# --------------------------------------------------------------------------- #
# 3) get_document
# --------------------------------------------------------------------------- #


class TestGetDocument:
    def test_happy_path(self, mock_repo):
        from app.services.documents_service import documents_service
        result = documents_service.get_document(MagicMock(), DOC_ID)
        assert result.id == DOC_ID

    def test_not_found(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.api.errors.exceptions import ApiNotFoundError
        mock_repo.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError, match="not found"):
            documents_service.get_document(MagicMock(), "missing-id")


# --------------------------------------------------------------------------- #
# 4) list_documents
# --------------------------------------------------------------------------- #


class TestListDocuments:
    def test_delegates_to_repo(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.api.query.models import ParsedListQuery

        query = MagicMock(spec=ParsedListQuery)
        mock_repo.list.return_value = ([_domain_doc(id="a"), _domain_doc(id="b")], 2)
        results, total = documents_service.list_documents(MagicMock(), query)
        assert total == 2
        assert len(results) == 2
        assert [r.id for r in results] == ["a", "b"]


# --------------------------------------------------------------------------- #
# 5) update_document
# --------------------------------------------------------------------------- #


class TestUpdateDocument:
    def test_no_op_when_no_updates(self, mock_repo):
        """has_updates()=False 인 request 는 현재 문서를 반환만 한다."""
        from app.services.documents_service import documents_service

        # 모든 필드 None → has_updates() False
        req = _update_req()
        result = documents_service.update_document(MagicMock(), DOC_ID, req)
        assert result.id == DOC_ID
        # repo.update 는 호출되지 않음
        assert not mock_repo.update.called

    def test_updates_title_only(self, mock_repo):
        from app.services.documents_service import documents_service
        req = _update_req(title="새 제목")
        mock_repo.update.return_value = _domain_doc(title="새 제목")

        result = documents_service.update_document(
            MagicMock(), DOC_ID, req, actor_id="a",
        )
        assert result.title == "새 제목"
        kw = mock_repo.update.call_args.kwargs
        assert kw["title"] == "새 제목"
        # metadata 는 None (수정하지 않음)
        assert kw["metadata"] is None

    def test_metadata_replace_revalidates_schema(self, mock_repo):
        """metadata 가 None 아니면 스키마 재검증 — 조회 + validate 호출."""
        from app.services.documents_service import documents_service

        # 먼저 get_document 에서 현재 document_type 조회. 그 후 schema fetch
        # conn 한 번 쓰이므로 fetchone 을 여러 호출에 대응시킨다.
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)  # schema_fields 없음 → 검증 skip
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        req = _update_req(metadata={"k": "v"})
        documents_service.update_document(conn, DOC_ID, req, actor_id="a")

        # get_document 이후 update 호출됨
        assert mock_repo.update.called
        kw = mock_repo.update.call_args.kwargs
        assert kw["metadata"] == {"k": "v"}

    def test_update_not_found(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.api.errors.exceptions import ApiNotFoundError

        mock_repo.update.return_value = None
        req = _update_req(title="x")
        with pytest.raises(ApiNotFoundError):
            documents_service.update_document(MagicMock(), DOC_ID, req)
