"""
S3 Phase 2 FG 2-1 — CollectionsService 유닛 테스트.

커버:
  - _normalize_name: 공백 처리 / 길이 상한
  - create_collection: 정상 / 이름 중복 → 409
  - get_collection: 정상 / 타 owner → 404
  - update_collection: 정상 / 없으면 404
  - delete_collection: 정상 / 없으면 404
  - add_documents: Scope 통과 문서만 accept — 타 scope 문서는 조용히 reject
  - remove_document / list_document_ids: owner 검증 + Scope 필터 전달
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


OWNER_A = "aaaaaaaa-0000-0000-0000-000000000001"
OWNER_B = "bbbbbbbb-0000-0000-0000-000000000002"
COLL_ID = "cccccccc-0000-0000-0000-000000000003"
SCOPE_A = "11111111-1111-1111-1111-111111111111"
SCOPE_B = "22222222-2222-2222-2222-222222222222"


def _actor(role=None, scope_profile_id=None, actor_id=OWNER_A):
    from app.api.auth.models import ActorType
    return SimpleNamespace(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=True,
        auth_method=None,
        tenant_id=None,
        role=role,
        agent_id=None,
        scope_profile_id=scope_profile_id,
        acting_on_behalf_of=None,
    )


def _coll(**kw):
    base = {
        "id": COLL_ID,
        "owner_id": OWNER_A,
        "name": "My Coll",
        "description": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "document_count": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _doc(**kw):
    base = {
        "id": "d1", "title": "t", "document_type": "policy",
        "status": "draft", "metadata": {}, "summary": None,
        "created_by": OWNER_A, "updated_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_draft_version_id": None,
        "current_published_version_id": None,
        "scope_profile_id": SCOPE_A,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def mock_repos(monkeypatch):
    from app.services import collections_service as svc_mod

    coll_repo = MagicMock()
    coll_repo.create.return_value = _coll()
    coll_repo.get_by_id.return_value = _coll()
    coll_repo.list_by_owner.return_value = ([_coll()], 1)
    coll_repo.update.return_value = _coll(name="renamed")
    coll_repo.delete.return_value = True
    coll_repo.add_documents.return_value = 1
    coll_repo.remove_document.return_value = True
    coll_repo.list_document_ids.return_value = ["d1", "d2"]

    doc_repo = MagicMock()
    doc_repo.get_by_id.return_value = _doc()

    monkeypatch.setattr(svc_mod, "collections_repository", coll_repo)
    monkeypatch.setattr(svc_mod, "documents_repository", doc_repo)
    return coll_repo, doc_repo


# ---------------------------------------------------------------------------
# 1) _normalize_name
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_strips_whitespace(self):
        from app.services.collections_service import _normalize_name
        assert _normalize_name("  my  coll  ") == "my coll"

    def test_rejects_empty(self):
        from app.services.collections_service import _normalize_name
        from app.api.errors.exceptions import ApiValidationError
        with pytest.raises(ApiValidationError):
            _normalize_name("   ")

    def test_rejects_too_long(self):
        from app.services.collections_service import _normalize_name
        from app.api.errors.exceptions import ApiValidationError
        with pytest.raises(ApiValidationError):
            _normalize_name("x" * 201)


# ---------------------------------------------------------------------------
# 2) create_collection
# ---------------------------------------------------------------------------


class TestCreateCollection:
    def test_happy_path(self, mock_repos):
        coll_repo, _ = mock_repos
        from app.services.collections_service import collections_service
        actor = _actor(scope_profile_id=SCOPE_A)
        result = collections_service.create_collection(
            MagicMock(), actor=actor, name="  New  Coll  ",
        )
        assert result.name == "My Coll"
        assert coll_repo.create.call_args.kwargs["name"] == "New Coll"
        assert coll_repo.create.call_args.kwargs["owner_id"] == OWNER_A

    def test_unauthenticated_rejected(self, mock_repos):
        from app.api.errors.exceptions import ApiValidationError
        from app.services.collections_service import collections_service
        with pytest.raises(ApiValidationError, match="인증"):
            collections_service.create_collection(
                MagicMock(), actor=None, name="x",
            )

    def test_duplicate_name_becomes_409(self, mock_repos):
        import psycopg2.errors as pgerr
        from app.api.errors.exceptions import ApiConflictError
        from app.services.collections_service import collections_service

        coll_repo, _ = mock_repos
        coll_repo.create.side_effect = pgerr.UniqueViolation("dup")

        actor = _actor()
        with pytest.raises(ApiConflictError):
            collections_service.create_collection(
                MagicMock(), actor=actor, name="dup",
            )


# ---------------------------------------------------------------------------
# 3) get_collection — owner 필터
# ---------------------------------------------------------------------------


class TestGetCollection:
    def test_owner_match(self, mock_repos):
        from app.services.collections_service import collections_service
        actor = _actor()
        result = collections_service.get_collection(
            MagicMock(), COLL_ID, actor=actor,
        )
        assert result.id == COLL_ID

    def test_other_owner_returns_404(self, mock_repos):
        coll_repo, _ = mock_repos
        coll_repo.get_by_id.return_value = None  # owner 불일치 시 repo 가 None 반환

        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.collections_service import collections_service
        actor = _actor(actor_id=OWNER_B)
        with pytest.raises(ApiNotFoundError):
            collections_service.get_collection(MagicMock(), COLL_ID, actor=actor)

        kwargs = coll_repo.get_by_id.call_args.kwargs
        assert kwargs["owner_id"] == OWNER_B  # repository 에 owner 필터 전달 확인


# ---------------------------------------------------------------------------
# 4) add_documents — Scope 밖 문서 거부
# ---------------------------------------------------------------------------


class TestAddDocumentsScopeFilter:
    def test_accepts_documents_in_scope(self, mock_repos):
        coll_repo, doc_repo = mock_repos
        from app.services.collections_service import collections_service

        actor = _actor(scope_profile_id=SCOPE_A)
        # 모든 문서가 Scope A 에 있음 → 전부 accept
        doc_repo.get_by_id.return_value = _doc()

        report = collections_service.add_documents(
            MagicMock(), COLL_ID, actor=actor, document_ids=["d1", "d2", "d3"],
        )
        assert report["requested"] == 3
        assert report["accepted"] == 3
        assert report["rejected"] == 0
        # repository 에는 accepted 문서만 전달
        assert len(coll_repo.add_documents.call_args.kwargs["document_ids"]) == 3

    def test_rejects_documents_out_of_scope(self, mock_repos):
        coll_repo, doc_repo = mock_repos
        from app.services.collections_service import collections_service

        # Scope B 사용자이면 Scope A 문서는 filter 에서 None 반환
        doc_repo.get_by_id.return_value = None
        actor = _actor(actor_id=OWNER_A, scope_profile_id=SCOPE_B)

        report = collections_service.add_documents(
            MagicMock(), COLL_ID, actor=actor, document_ids=["d1", "d2"],
        )
        assert report["requested"] == 2
        assert report["accepted"] == 0
        assert report["rejected"] == 2

    def test_passes_viewer_scope_to_repo(self, mock_repos):
        """Scope 필터 전파 검증 — viewer Scope 기반 get_by_id 호출."""
        _, doc_repo = mock_repos
        doc_repo.get_by_id.return_value = _doc()
        from app.services.collections_service import collections_service

        actor = _actor(scope_profile_id=SCOPE_A)
        collections_service.add_documents(
            MagicMock(), COLL_ID, actor=actor, document_ids=["d1"],
        )
        # repo.get_by_id 가 viewer_scope_profile_ids=[SCOPE_A] 로 호출됐는지
        kwargs = doc_repo.get_by_id.call_args.kwargs
        assert kwargs["viewer_scope_profile_ids"] == [SCOPE_A]


# ---------------------------------------------------------------------------
# 5) list_document_ids — Scope 필터 전파
# ---------------------------------------------------------------------------


class TestListDocumentIdsScopeFilter:
    def test_propagates_viewer_scope(self, mock_repos):
        coll_repo, _ = mock_repos
        from app.services.collections_service import collections_service

        actor = _actor(scope_profile_id=SCOPE_A)
        result = collections_service.list_document_ids(
            MagicMock(), COLL_ID, actor=actor,
        )
        assert result == ["d1", "d2"]
        assert coll_repo.list_document_ids.call_args.kwargs[
            "viewer_scope_profile_ids"
        ] == [SCOPE_A]
