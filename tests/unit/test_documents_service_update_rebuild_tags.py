"""
S3 Phase 2 FG 2-2 BUG-FG22-01 회귀 — `documents_service.update_document`
경로가 metadata 교체 시 snapshot_sync_service.rebuild_tags_for_document 를
호출해 document_tags 테이블을 재계산하는지 검증.

Chrome 실측에서 발견: TagChipsEditor 가 PATCH /documents/{id} (metadata.tags)
로 프런트매터 태그를 저장했으나 document_tags 가 갱신되지 않아 UI 에 칩이
나타나지 않던 회귀.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


DOC_ID = "d2222222-2222-2222-2222-222222222222"
OWNER_A = "aaaaaaaa-0000-0000-0000-000000000002"
SCOPE_A = "ssssssss-0000-0000-0000-000000000002"
VERSION_ID = "v0000000-0000-0000-0000-000000000001"


def _actor(role="AUTHOR", scope_profile_id=SCOPE_A, actor_id=OWNER_A):
    from app.api.auth.models import ActorContext, ActorType
    return ActorContext(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=True,
        auth_method=None,
        tenant_id=None,
        role=role,
        scope_profile_id=scope_profile_id,
    )


def _doc_row(metadata=None, **kw):
    from app.models.document import Document
    base = dict(
        id=DOC_ID,
        title="t",
        document_type="policy",
        status="draft",
        metadata=metadata if metadata is not None else {},
        summary=None,
        created_by=OWNER_A,
        updated_by=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        current_draft_version_id=VERSION_ID,
        current_published_version_id=None,
        scope_profile_id=SCOPE_A,
    )
    base.update(kw)
    return Document(**base)


@pytest.fixture
def mock_stack(monkeypatch):
    """update_document 가 의존하는 repository/service 경로 전수 모킹.

    - documents_repository.update → updated Document 반환
    - documents_service.get_document (ACL + current 조회) → stub
    - versions_repository.get_by_id → content_snapshot 가진 Version
    - tags_repository.list_for_document → [] (get_document 확장 부분)
    - collections/folders repositories → [] / None
    - snapshot_sync_service.rebuild_tags_for_document → MagicMock (호출 여부/인자 관찰용)
    """
    from app.services import documents_service as svc_mod

    # 1) documents_repository
    doc_repo = MagicMock()
    # get_document 내부 get_by_id + update 가 각각 호출되므로 side_effect 로 같은 row 반환
    doc_repo.get_by_id.return_value = _doc_row()
    doc_repo.update.return_value = _doc_row(metadata={"tags": ["ai", "ml"]})
    monkeypatch.setattr(svc_mod, "documents_repository", doc_repo)

    # 2) _validate_metadata_against_schema 는 update_document 가 직접 호출 — 비활성화
    monkeypatch.setattr(svc_mod, "_validate_metadata_against_schema", lambda *a, **k: None)

    # 3) collections / folders / tags repositories (get_document 의 확장부) — lazy import 패치
    import app.repositories.collections_repository as coll_mod
    import app.repositories.folders_repository as folder_mod
    import app.repositories.tags_repository as tag_mod
    coll_repo = MagicMock()
    coll_repo.list_collection_ids_for_document.return_value = []
    folder_repo = MagicMock()
    folder_repo.get_folder_of_document.return_value = None
    tag_repo_get = MagicMock()
    tag_repo_get.list_for_document.return_value = []
    monkeypatch.setattr(coll_mod, "collections_repository", coll_repo)
    monkeypatch.setattr(folder_mod, "folders_repository", folder_repo)
    monkeypatch.setattr(tag_mod, "tags_repository", tag_repo_get)

    # 4) versions_repository.get_by_id — lazy import (update_document 안에서)
    import app.repositories.versions_repository as ver_mod
    ver_repo = MagicMock()
    ver_repo.get_by_id.return_value = SimpleNamespace(
        id=VERSION_ID,
        content_snapshot={"type": "doc", "content": []},
    )
    monkeypatch.setattr(ver_mod, "versions_repository", ver_repo)

    # 5) rebuild_tags_for_document — lazy import (update_document 안에서)
    import app.services.snapshot_sync_service as sync_mod
    rebuild = MagicMock(return_value=[("ai", "frontmatter"), ("ml", "frontmatter")])
    monkeypatch.setattr(sync_mod, "rebuild_tags_for_document", rebuild)

    return SimpleNamespace(
        doc_repo=doc_repo,
        ver_repo=ver_repo,
        rebuild=rebuild,
    )


class TestUpdateDocumentRebuildsTags:
    def test_metadata_change_triggers_rebuild_tags(self, mock_stack):
        """metadata 교체 시 rebuild_tags_for_document 호출 + snapshot + metadata 전달."""
        from app.schemas.documents import DocumentUpdateRequest
        from app.services.documents_service import documents_service

        req = DocumentUpdateRequest(metadata={"tags": ["ai", "ml"]})
        documents_service.update_document(
            MagicMock(), DOC_ID, req, actor_id=OWNER_A, actor=_actor(),
        )

        assert mock_stack.rebuild.call_count == 1
        _, kwargs = mock_stack.rebuild.call_args
        assert kwargs["document_id"] == DOC_ID
        assert kwargs["metadata"] == {"tags": ["ai", "ml"]}
        # 현재 활성 draft 스냅샷이 전달되어 인라인 태그도 함께 재계산
        assert kwargs["snapshot"] == {"type": "doc", "content": []}

    def test_metadata_none_skips_rebuild(self, mock_stack):
        """metadata 가 None (즉 title 만 변경) 인 경우 rebuild 호출 안 됨."""
        from app.schemas.documents import DocumentUpdateRequest
        from app.services.documents_service import documents_service

        req = DocumentUpdateRequest(title="new title")
        documents_service.update_document(
            MagicMock(), DOC_ID, req, actor_id=OWNER_A, actor=_actor(),
        )

        mock_stack.rebuild.assert_not_called()

    def test_no_updates_is_noop(self, mock_stack):
        """빈 DocumentUpdateRequest 는 rebuild 도 update 도 호출 안 함."""
        from app.schemas.documents import DocumentUpdateRequest
        from app.services.documents_service import documents_service

        req = DocumentUpdateRequest()  # all None
        documents_service.update_document(
            MagicMock(), DOC_ID, req, actor_id=OWNER_A, actor=_actor(),
        )

        mock_stack.rebuild.assert_not_called()
        mock_stack.doc_repo.update.assert_not_called()

    def test_rebuild_without_active_version_passes_none_snapshot(self, monkeypatch, mock_stack):
        """활성 version 이 없는 문서 (초기 생성 직후 등) 는 snapshot=None 로 호출 — 서비스가 크래시 없이 metadata 만으로 재계산."""
        import app.repositories.versions_repository as ver_mod
        from app.services import documents_service as svc_mod

        # current_draft_version_id / current_published_version_id 둘 다 없는 doc
        no_version_doc = _doc_row(
            metadata={"tags": ["ai"]},
            current_draft_version_id=None,
            current_published_version_id=None,
        )
        svc_mod.documents_repository.update.return_value = no_version_doc

        ver_mod.versions_repository.get_by_id = MagicMock()  # 호출 안 되어야 함

        from app.schemas.documents import DocumentUpdateRequest
        from app.services.documents_service import documents_service
        documents_service.update_document(
            MagicMock(), DOC_ID, DocumentUpdateRequest(metadata={"tags": ["ai"]}),
            actor_id=OWNER_A, actor=_actor(),
        )

        ver_mod.versions_repository.get_by_id.assert_not_called()
        assert mock_stack.rebuild.call_count == 1
        _, kwargs = mock_stack.rebuild.call_args
        assert kwargs["snapshot"] is None
        assert kwargs["metadata"] == {"tags": ["ai"]}

    def test_rebuild_when_version_row_missing_passes_none(self, mock_stack):
        """version id 는 있지만 versions row 가 사라진 엣지 — snapshot=None 안전하게 처리."""
        mock_stack.ver_repo.get_by_id.return_value = None

        from app.schemas.documents import DocumentUpdateRequest
        from app.services.documents_service import documents_service
        documents_service.update_document(
            MagicMock(), DOC_ID, DocumentUpdateRequest(metadata={"tags": ["ai"]}),
            actor_id=OWNER_A, actor=_actor(),
        )

        assert mock_stack.rebuild.call_count == 1
        _, kwargs = mock_stack.rebuild.call_args
        assert kwargs["snapshot"] is None
