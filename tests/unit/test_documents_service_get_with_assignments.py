"""
S3 Phase 2 FG 2-1 UX 2차 회귀 — DocumentsService.get_document 가 actor 전달 시
현재 배치된 폴더와 요청자 소유 컬렉션 포함 목록을 함께 반환하는지 검증.
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


DOC_ID = "d1111111-1111-1111-1111-111111111111"
OWNER_A = "aaaaaaaa-0000-0000-0000-000000000001"
SCOPE_A = "ssssssss-0000-0000-0000-000000000001"
FOLDER_ID = "ffffffff-0000-0000-0000-000000000001"
COLL_1 = "cccccccc-0000-0000-0000-000000000001"
COLL_2 = "cccccccc-0000-0000-0000-000000000002"


def _actor(role="VIEWER", scope_profile_id=SCOPE_A, actor_id=OWNER_A):
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


def _doc_row(**kw):
    from app.models.document import Document
    base = dict(
        id=DOC_ID,
        title="t",
        document_type="policy",
        status="draft",
        metadata={},
        summary=None,
        created_by=OWNER_A,
        updated_by=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        current_draft_version_id=None,
        current_published_version_id=None,
        scope_profile_id=SCOPE_A,
    )
    base.update(kw)
    return Document(**base)


@pytest.fixture
def mock_repos(monkeypatch):
    from app.services import documents_service as svc_mod

    # documents_repository 는 서비스 상단에서 이미 import 돼 있음
    doc_repo = MagicMock()
    doc_repo.get_by_id.return_value = _doc_row()
    monkeypatch.setattr(svc_mod, "documents_repository", doc_repo)

    # collections/folders repository 는 get_document 안에서 lazy import → 해당 모듈을 패치
    coll_repo = MagicMock()
    coll_repo.list_collection_ids_for_document.return_value = [COLL_1, COLL_2]
    folder_repo = MagicMock()
    folder_repo.get_folder_of_document.return_value = FOLDER_ID
    # S3 Phase 2 FG 2-2: tags_repository 도 lazy import 되므로 같은 패턴으로 패치
    tag_repo = MagicMock()
    tag_repo.list_for_document.return_value = []

    import app.repositories.collections_repository as coll_mod
    import app.repositories.folders_repository as folder_mod
    import app.repositories.tags_repository as tag_mod
    monkeypatch.setattr(coll_mod, "collections_repository", coll_repo)
    monkeypatch.setattr(folder_mod, "folders_repository", folder_repo)
    monkeypatch.setattr(tag_mod, "tags_repository", tag_repo)

    return doc_repo, coll_repo, folder_repo


class TestGetDocumentTags:
    def test_tags_populated_when_actor_present(self, monkeypatch, mock_repos):
        _doc_repo, _coll_repo, _folder_repo = mock_repos
        from types import SimpleNamespace
        import app.repositories.tags_repository as tag_mod
        tag_a = SimpleNamespace(id="t1", name_normalized="ai", created_at=None, usage_count=None)
        tag_b = SimpleNamespace(id="t2", name_normalized="ml", created_at=None, usage_count=None)
        tag_mod.tags_repository.list_for_document.return_value = [
            (tag_a, "inline"),
            (tag_b, "both"),
        ]

        from app.services.documents_service import documents_service
        res = documents_service.get_document(MagicMock(), DOC_ID, actor=_actor())
        assert res.document_tags == [
            {"id": "t1", "name": "ai", "source": "inline"},
            {"id": "t2", "name": "ml", "source": "both"},
        ]

    def test_tags_empty_when_no_actor(self, mock_repos):
        from app.services.documents_service import documents_service
        res = documents_service.get_document(MagicMock(), DOC_ID, actor=None)
        assert res.document_tags == []


class TestGetDocumentAssignments:
    def test_includes_folder_and_collections_when_actor_present(self, mock_repos):
        _doc_repo, coll_repo, folder_repo = mock_repos
        from app.services.documents_service import documents_service

        actor = _actor()
        res = documents_service.get_document(MagicMock(), DOC_ID, actor=actor)

        assert res.folder_id == FOLDER_ID
        assert res.in_collection_ids == [COLL_1, COLL_2]

        # owner 필터 전달 확인
        kwargs = coll_repo.list_collection_ids_for_document.call_args.kwargs
        assert kwargs["owner_id"] == OWNER_A
        assert kwargs["document_id"] == DOC_ID
        folder_repo.get_folder_of_document.assert_called_once()

    def test_empty_assignments_when_none_placed(self, mock_repos):
        _doc_repo, coll_repo, folder_repo = mock_repos
        coll_repo.list_collection_ids_for_document.return_value = []
        folder_repo.get_folder_of_document.return_value = None

        from app.services.documents_service import documents_service
        res = documents_service.get_document(MagicMock(), DOC_ID, actor=_actor())
        assert res.folder_id is None
        assert res.in_collection_ids == []

    def test_no_actor_leaves_defaults(self, mock_repos):
        _doc_repo, coll_repo, folder_repo = mock_repos
        from app.services.documents_service import documents_service
        res = documents_service.get_document(MagicMock(), DOC_ID, actor=None)
        # 레거시/내부 호출은 배치 상태 조회를 생략 (성능)
        assert res.folder_id is None
        assert res.in_collection_ids == []
        coll_repo.list_collection_ids_for_document.assert_not_called()
        folder_repo.get_folder_of_document.assert_not_called()

    def test_scope_filter_still_enforced(self, mock_repos):
        """Scope 밖 문서는 여전히 404 — assignments 단계에 닿기 전 차단."""
        doc_repo, _coll_repo, _folder_repo = mock_repos
        doc_repo.get_by_id.return_value = None

        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.documents_service import documents_service
        with pytest.raises(ApiNotFoundError):
            documents_service.get_document(MagicMock(), "missing", actor=_actor())
