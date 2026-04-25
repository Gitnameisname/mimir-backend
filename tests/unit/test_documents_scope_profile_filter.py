"""
S3 Phase 2 FG 2-0 회귀 테스트 — documents.scope_profile_id ACL 필터.

Pre-flight 실측 §2, §6 에서 확인된 바와 같이, Phase 2 직전까지 documents 조회
경로에 Scope Profile 기반 ACL 필터가 적용되지 않았다. 본 테스트는 FG 2-0 이후
다음 불변식을 검증한다:

  (1) `_build_list_query` 가 viewer_scope_profile_ids 를 받아 SQL WHERE 절을
      올바르게 구성한다 (None=skip / []=1=0 / [ids]=IN).
  (2) `DocumentsService._resolve_viewer_scope_profile_ids` 가 ActorContext 를
      규약에 맞게 치환한다 (admin role = None / 일반 user = [id] / scope 없음 = []).
  (3) `DocumentsService.list_documents / get_document / update_document` 가
      actor 를 전달받아 repository 에 필터를 내려보낸다.
  (4) Scope 밖 문서 단건 조회는 404 (403 아님 — 존재 유출 방지).
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

from app.api.query.models import ParsedListQuery
from app.repositories.documents_repository import _build_list_query


pytestmark = pytest.mark.unit

DOC_ID = "d2222222-2222-2222-2222-222222222222"
SCOPE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SCOPE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# 1) _build_list_query — viewer_scope_profile_ids 파라미터
# ---------------------------------------------------------------------------


class TestBuildListQueryScopeFilter:
    def _run(self, *, viewer_ids):
        q = ParsedListQuery()
        return _build_list_query(q, viewer_scope_profile_ids=viewer_ids)

    def test_none_skips_filter(self):
        data_sql, data_params, count_sql, count_params = self._run(viewer_ids=None)
        # SELECT 컬럼 목록에는 scope_profile_id 가 항상 존재 (반환 시 필요).
        # "필터 skip" 의 의미는 WHERE 절에 scope 조건이 더해지지 않았다는 것.
        assert "scope_profile_id IN" not in data_sql
        assert "1 = 0" not in data_sql
        # 다른 filter 도 없으니 WHERE 절 자체가 없어야 함
        assert "WHERE" not in data_sql
        # count_sql 에는 SELECT COUNT(*) 뿐이므로 scope_profile_id 가 아예 없다.
        assert "scope_profile_id" not in count_sql

    def test_empty_list_blocks_all_rows(self):
        data_sql, data_params, _, count_params = self._run(viewer_ids=[])
        assert "1 = 0" in data_sql
        # scope_profile_id IN (...) 절은 없어야 함
        assert "scope_profile_id IN" not in data_sql
        # params 에는 추가 인자가 실리지 않음 (LIMIT, OFFSET 만)
        assert data_params[-2:] == [20, 0]  # default page_size, offset
        # "1 = 0" 은 리터럴이라 파라미터 바인딩 없이도 COUNT 가 0
        assert count_params == []

    def test_single_id_generates_in_clause(self):
        data_sql, data_params, _, count_params = self._run(viewer_ids=[SCOPE_A])
        assert "scope_profile_id IN (%s)" in data_sql
        assert SCOPE_A in data_params
        assert SCOPE_A in count_params

    def test_multiple_ids_generate_in_clause(self):
        data_sql, data_params, _, _ = self._run(viewer_ids=[SCOPE_A, SCOPE_B])
        assert "scope_profile_id IN (%s, %s)" in data_sql
        assert SCOPE_A in data_params and SCOPE_B in data_params

    def test_combines_with_existing_filter(self):
        q = ParsedListQuery(filters={"status": "published"})
        data_sql, data_params, _, _ = _build_list_query(
            q, viewer_scope_profile_ids=[SCOPE_A],
        )
        # 두 조건이 AND 로 결합되어야 함
        assert "status = %s" in data_sql
        assert "scope_profile_id IN (%s)" in data_sql
        assert "AND" in data_sql
        # 순서: status 먼저 (filters.items() 순서), scope 다음
        assert data_params[:3] == ["published", SCOPE_A, 20] or data_params[:3] == [
            "published",
            SCOPE_A,
            20,
        ]


# ---------------------------------------------------------------------------
# 2) _resolve_viewer_scope_profile_ids — ActorContext → viewer set
# ---------------------------------------------------------------------------


def _actor(role=None, scope_profile_id=None, actor_type="user"):
    """가벼운 ActorContext mock — 실제 dataclass 생성 없이 attribute 만 흉내."""
    from app.api.auth.models import ActorType
    return SimpleNamespace(
        actor_type=ActorType.USER if actor_type == "user" else ActorType.ANONYMOUS,
        actor_id="u-1",
        is_authenticated=True,
        auth_method=None,
        tenant_id=None,
        role=role,
        agent_id=None,
        scope_profile_id=scope_profile_id,
        acting_on_behalf_of=None,
    )


class TestResolveViewerScopeProfileIds:
    def test_none_actor_returns_none(self):
        from app.services.documents_service import _resolve_viewer_scope_profile_ids
        assert _resolve_viewer_scope_profile_ids(None) is None

    def test_super_admin_bypass(self):
        from app.services.documents_service import _resolve_viewer_scope_profile_ids
        actor = _actor(role="SUPER_ADMIN", scope_profile_id=SCOPE_A)
        assert _resolve_viewer_scope_profile_ids(actor) is None

    def test_org_admin_bypass(self):
        from app.services.documents_service import _resolve_viewer_scope_profile_ids
        actor = _actor(role="ORG_ADMIN", scope_profile_id=SCOPE_A)
        assert _resolve_viewer_scope_profile_ids(actor) is None

    def test_regular_user_returns_single_id(self):
        from app.services.documents_service import _resolve_viewer_scope_profile_ids
        actor = _actor(role="VIEWER", scope_profile_id=SCOPE_A)
        assert _resolve_viewer_scope_profile_ids(actor) == [SCOPE_A]

    def test_user_without_scope_returns_empty(self):
        """scope_profile_id 가 없는 사용자 → 결과 없음 (S2 ⑥ 차단)."""
        from app.services.documents_service import _resolve_viewer_scope_profile_ids
        actor = _actor(role="VIEWER", scope_profile_id=None)
        assert _resolve_viewer_scope_profile_ids(actor) == []


# ---------------------------------------------------------------------------
# 3) DocumentsService — actor 전달 시 repository 필터 인자 호출 검증
# ---------------------------------------------------------------------------


def _domain_doc(**kw):
    base = {
        "id": DOC_ID,
        "title": "T",
        "document_type": "policy",
        "status": "draft",
        "metadata": {},
        "summary": None,
        "created_by": "author-1",
        "updated_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "current_draft_version_id": None,
        "current_published_version_id": None,
        "scope_profile_id": SCOPE_A,
        # S3 Phase 2 FG 2-1 UX 2차 (2026-04-24) 이후 Document dataclass 에 추가된 필드.
        # `_to_response(doc)` 가 이 두 attribute 를 읽으므로 기본값이 필요.
        "folder_id": None,
        "in_collection_ids": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def mock_repo(monkeypatch):
    from app.services import documents_service as svc_mod

    repo = MagicMock()
    repo.get_by_id.return_value = _domain_doc()
    repo.create.return_value = _domain_doc(id="new-doc")
    repo.update.return_value = _domain_doc(title="updated")
    repo.list.return_value = ([_domain_doc()], 1)

    monkeypatch.setattr(svc_mod, "documents_repository", repo)
    return repo


class TestServicePassesActorToRepo:
    def test_list_without_actor_uses_none_filter(self, mock_repo):
        from app.services.documents_service import documents_service
        documents_service.list_documents(MagicMock(), ParsedListQuery())
        mock_repo.list.assert_called_once()
        assert mock_repo.list.call_args.kwargs["viewer_scope_profile_ids"] is None

    def test_list_with_regular_user_narrows(self, mock_repo):
        from app.services.documents_service import documents_service
        actor = _actor(role="VIEWER", scope_profile_id=SCOPE_A)
        documents_service.list_documents(MagicMock(), ParsedListQuery(), actor=actor)
        assert mock_repo.list.call_args.kwargs["viewer_scope_profile_ids"] == [SCOPE_A]

    def test_list_with_admin_skips_filter(self, mock_repo):
        from app.services.documents_service import documents_service
        actor = _actor(role="SUPER_ADMIN", scope_profile_id=SCOPE_A)
        documents_service.list_documents(MagicMock(), ParsedListQuery(), actor=actor)
        assert mock_repo.list.call_args.kwargs["viewer_scope_profile_ids"] is None

    def test_list_with_user_no_scope_empties(self, mock_repo):
        from app.services.documents_service import documents_service
        actor = _actor(role="VIEWER", scope_profile_id=None)
        documents_service.list_documents(MagicMock(), ParsedListQuery(), actor=actor)
        assert mock_repo.list.call_args.kwargs["viewer_scope_profile_ids"] == []

    def test_get_with_mismatched_scope_raises_404(self, mock_repo):
        """scope 가 다르면 repo 가 None 을 반환하도록 흉내 → service 가 404."""
        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.documents_service import documents_service

        mock_repo.get_by_id.return_value = None  # 필터 통과 못한 효과
        actor = _actor(role="VIEWER", scope_profile_id=SCOPE_B)
        with pytest.raises(ApiNotFoundError):
            documents_service.get_document(MagicMock(), DOC_ID, actor=actor)
        kwargs = mock_repo.get_by_id.call_args.kwargs
        assert kwargs["viewer_scope_profile_ids"] == [SCOPE_B]

    def test_create_injects_scope_from_actor(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.schemas.documents import DocumentCreateRequest

        actor = _actor(role="AUTHOR", scope_profile_id=SCOPE_A)
        req = DocumentCreateRequest(title="t", document_type="policy")

        # 스키마 검증 스킵 (document_types.schema_fields 없음)
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        conn.cursor = MagicMock(return_value=cur)

        documents_service.create_document(conn, req, actor_id="u-1", actor=actor)
        assert mock_repo.create.call_args.kwargs["scope_profile_id"] == SCOPE_A

    def test_create_without_actor_leaves_scope_null(self, mock_repo):
        from app.services.documents_service import documents_service
        from app.schemas.documents import DocumentCreateRequest

        req = DocumentCreateRequest(title="t", document_type="policy")

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        conn.cursor = MagicMock(return_value=cur)

        documents_service.create_document(conn, req, actor_id="u-1")  # actor 전달 안 함
        assert mock_repo.create.call_args.kwargs["scope_profile_id"] is None

    def test_update_enforces_acl_before_mutation(self, mock_repo):
        """Scope 밖 문서 update 시도 → get 단계에서 404, update 호출 안 됨."""
        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.documents_service import documents_service
        from app.schemas.documents import DocumentUpdateRequest

        mock_repo.get_by_id.return_value = None
        actor = _actor(role="VIEWER", scope_profile_id=SCOPE_B)
        req = DocumentUpdateRequest(title="bad")

        with pytest.raises(ApiNotFoundError):
            documents_service.update_document(
                MagicMock(), DOC_ID, req, actor_id="u-1", actor=actor,
            )
        # update 는 호출되지 않아야 함 (get 에서 이미 차단)
        assert not mock_repo.update.called


# ---------------------------------------------------------------------------
# 4) Document 도메인 모델 — 새 필드 확인
# ---------------------------------------------------------------------------


class TestDocumentModelField:
    def test_default_scope_profile_id_is_none(self):
        from app.models.document import Document
        doc = Document(
            id="1",
            title="t",
            document_type="policy",
            status="draft",
            metadata={},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert doc.scope_profile_id is None

    def test_explicit_scope_profile_id(self):
        from app.models.document import Document
        doc = Document(
            id="1",
            title="t",
            document_type="policy",
            status="draft",
            metadata={},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            scope_profile_id=SCOPE_A,
        )
        assert doc.scope_profile_id == SCOPE_A
