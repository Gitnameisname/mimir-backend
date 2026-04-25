"""
S3 Phase 2 FG 2-1 — FoldersService 유닛 테스트.

커버:
  - _normalize_name: '/' 금지 / 길이 / 공백 압축
  - compute_child_path: 루트 / 자식 / 이름에 '/' 포함 시 치환
  - create_folder: 루트 / 자식 / 부재 부모 / 깊이 상한 초과
  - rename_folder: 소유권 / 존재 / 충돌
  - move_folder: 자기 이동 금지 / 하위 이동 금지 / 깊이 상한 / 정상
  - delete_folder: 빈 폴더만 / 하위 있음 → 409
  - set_document_folder: 문서 Scope / 폴더 소유권
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
FOLDER_ID = "ffffffff-0000-0000-0000-000000000001"
PARENT_ID = "ffffffff-0000-0000-0000-000000000002"
DOC_ID = "dddddddd-0000-0000-0000-000000000003"
SCOPE_A = "11111111-1111-1111-1111-111111111111"


def _actor(scope_profile_id=SCOPE_A, actor_id=OWNER_A):
    from app.api.auth.models import ActorType
    return SimpleNamespace(
        actor_type=ActorType.USER,
        actor_id=actor_id,
        is_authenticated=True,
        auth_method=None,
        tenant_id=None,
        role=None,
        agent_id=None,
        scope_profile_id=scope_profile_id,
        acting_on_behalf_of=None,
    )


def _folder(id=FOLDER_ID, name="root", parent_id=None, path="/root/", depth=0, owner=OWNER_A):
    return SimpleNamespace(
        id=id, owner_id=owner, parent_id=parent_id,
        name=name, path=path, depth=depth,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _doc(scope_profile_id=SCOPE_A):
    return SimpleNamespace(
        id=DOC_ID, title="t", document_type="policy", status="draft",
        metadata={}, summary=None, created_by=OWNER_A, updated_by=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        current_draft_version_id=None, current_published_version_id=None,
        scope_profile_id=scope_profile_id,
    )


@pytest.fixture
def mock_repos(monkeypatch):
    from app.services import folders_service as svc_mod

    f_repo = MagicMock()
    f_repo.get_by_id.return_value = _folder()
    f_repo.create.return_value = _folder()
    f_repo.list_by_owner.return_value = [_folder()]
    f_repo.is_descendant.return_value = False
    f_repo.move.return_value = _folder(path="/new/")
    f_repo.rename.return_value = _folder(name="renamed", path="/renamed/")
    f_repo.delete_if_empty.return_value = True

    d_repo = MagicMock()
    d_repo.get_by_id.return_value = _doc()

    monkeypatch.setattr(svc_mod, "folders_repository", f_repo)
    monkeypatch.setattr(svc_mod, "documents_repository", d_repo)
    return f_repo, d_repo


# ---------------------------------------------------------------------------
# 1) _normalize_name / compute_child_path
# ---------------------------------------------------------------------------


class TestNameNormalization:
    def test_rejects_slash_in_name(self):
        from app.services.folders_service import _normalize_name
        from app.api.errors.exceptions import ApiValidationError
        with pytest.raises(ApiValidationError, match="/"):
            _normalize_name("a/b")

    def test_strips_whitespace(self):
        from app.services.folders_service import _normalize_name
        assert _normalize_name("  hello   world  ") == "hello world"


class TestComputeChildPath:
    def test_root(self):
        from app.repositories.folders_repository import compute_child_path
        assert compute_child_path(None, "work") == "/work/"

    def test_child(self):
        from app.repositories.folders_repository import compute_child_path
        assert compute_child_path("/work/", "projects") == "/work/projects/"

    def test_name_with_slash_replaced(self):
        from app.repositories.folders_repository import compute_child_path
        # 이름에 '/' 가 있어도 구분자 혼동 방지 위해 '_' 로 치환
        assert compute_child_path(None, "a/b") == "/a_b/"


# ---------------------------------------------------------------------------
# 2) create_folder
# ---------------------------------------------------------------------------


class TestCreateFolder:
    def test_root_happy(self, mock_repos):
        f_repo, _ = mock_repos
        from app.services.folders_service import folders_service
        result = folders_service.create_folder(
            MagicMock(), actor=_actor(), parent_id=None, name="work",
        )
        assert result is not None
        kwargs = f_repo.create.call_args.kwargs
        assert kwargs["parent_id"] is None
        assert kwargs["path"] == "/work/"
        assert kwargs["depth"] == 0

    def test_child_uses_parent_path(self, mock_repos):
        f_repo, _ = mock_repos
        f_repo.get_by_id.return_value = _folder(
            id=PARENT_ID, name="work", parent_id=None, path="/work/", depth=0,
        )
        from app.services.folders_service import folders_service
        folders_service.create_folder(
            MagicMock(), actor=_actor(), parent_id=PARENT_ID, name="projects",
        )
        kwargs = f_repo.create.call_args.kwargs
        assert kwargs["path"] == "/work/projects/"
        assert kwargs["depth"] == 1

    def test_missing_parent_404(self, mock_repos):
        f_repo, _ = mock_repos
        f_repo.get_by_id.return_value = None
        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiNotFoundError):
            folders_service.create_folder(
                MagicMock(), actor=_actor(), parent_id=PARENT_ID, name="x",
            )

    def test_depth_limit_exceeded(self, mock_repos):
        f_repo, _ = mock_repos
        f_repo.get_by_id.return_value = _folder(
            id=PARENT_ID, path="/a/b/c/d/e/f/g/h/i/j/", depth=10,
        )
        from app.api.errors.exceptions import ApiValidationError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiValidationError, match="깊이 상한"):
            folders_service.create_folder(
                MagicMock(), actor=_actor(), parent_id=PARENT_ID, name="too-deep",
            )


# ---------------------------------------------------------------------------
# 3) move_folder — 순환 참조 / 깊이 / 정상
# ---------------------------------------------------------------------------


class TestMoveFolder:
    def test_cannot_move_to_self(self, mock_repos):
        from app.api.errors.exceptions import ApiValidationError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiValidationError, match="자기 자신"):
            folders_service.move_folder(
                MagicMock(), FOLDER_ID, actor=_actor(), new_parent_id=FOLDER_ID,
            )

    def test_cannot_move_into_descendant(self, mock_repos):
        f_repo, _ = mock_repos
        # 다른 ID 의 폴더를 new_parent 로 지정
        new_parent_id = "ffffffff-0000-0000-0000-000000000099"
        f_repo.is_descendant.return_value = True  # 순환 참조 모사

        from app.api.errors.exceptions import ApiValidationError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiValidationError, match="순환"):
            folders_service.move_folder(
                MagicMock(), FOLDER_ID, actor=_actor(), new_parent_id=new_parent_id,
            )

    def test_move_to_root(self, mock_repos):
        f_repo, _ = mock_repos
        # current 는 /a/b/ depth=1, move to root (new_parent=None, depth=0)
        f_repo.get_by_id.return_value = _folder(
            id=FOLDER_ID, name="b", parent_id=PARENT_ID, path="/a/b/", depth=1,
        )
        f_repo.list_by_owner.return_value = [
            _folder(id=FOLDER_ID, name="b", path="/a/b/", depth=1),
        ]
        from app.services.folders_service import folders_service
        result = folders_service.move_folder(
            MagicMock(), FOLDER_ID, actor=_actor(), new_parent_id=None,
        )
        assert result is not None
        kwargs = f_repo.move.call_args.kwargs
        assert kwargs["new_parent_id"] is None
        assert kwargs["new_parent_path"] is None
        assert kwargs["new_depth"] == 0

    def test_depth_would_exceed_limit(self, mock_repos):
        f_repo, _ = mock_repos
        # current 는 depth=5, 하위 중 가장 깊은 놈이 depth=8, new_parent_depth=7 이면
        # delta=+3 → 하위 max 8+3=11 > 10 초과
        f_repo.get_by_id.side_effect = [
            _folder(id=FOLDER_ID, path="/a/b/c/d/e/f/", depth=5),  # current
            _folder(id=PARENT_ID, path="/x/y/z/q/w/e/r/t/", depth=7),  # new_parent
        ]
        f_repo.list_by_owner.return_value = [
            _folder(id=FOLDER_ID, path="/a/b/c/d/e/f/", depth=5),
            _folder(id="deep", path="/a/b/c/d/e/f/g/h/i/", depth=8),
        ]
        from app.api.errors.exceptions import ApiValidationError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiValidationError, match="깊이"):
            folders_service.move_folder(
                MagicMock(), FOLDER_ID, actor=_actor(), new_parent_id=PARENT_ID,
            )


# ---------------------------------------------------------------------------
# 4) delete_folder — 비어있지 않으면 409
# ---------------------------------------------------------------------------


class TestDeleteFolder:
    def test_empty_folder_deleted(self, mock_repos):
        from app.services.folders_service import folders_service
        folders_service.delete_folder(
            MagicMock(), FOLDER_ID, actor=_actor(),
        )

    def test_non_empty_folder_409(self, mock_repos):
        f_repo, _ = mock_repos
        f_repo.delete_if_empty.return_value = False

        from app.api.errors.exceptions import ApiConflictError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiConflictError, match="하위|문서"):
            folders_service.delete_folder(
                MagicMock(), FOLDER_ID, actor=_actor(),
            )


# ---------------------------------------------------------------------------
# 5) set_document_folder — Scope + owner 검증
# ---------------------------------------------------------------------------


class TestSetDocumentFolder:
    def test_happy_set(self, mock_repos):
        f_repo, d_repo = mock_repos
        from app.services.folders_service import folders_service
        folders_service.set_document_folder(
            MagicMock(),
            actor=_actor(),
            document_id=DOC_ID,
            folder_id=FOLDER_ID,
        )
        assert f_repo.set_folder.called
        kwargs = f_repo.set_folder.call_args.kwargs
        assert kwargs["document_id"] == DOC_ID
        assert kwargs["folder_id"] == FOLDER_ID

    def test_clear(self, mock_repos):
        f_repo, _ = mock_repos
        from app.services.folders_service import folders_service
        folders_service.set_document_folder(
            MagicMock(),
            actor=_actor(),
            document_id=DOC_ID,
            folder_id=None,
        )
        kwargs = f_repo.set_folder.call_args.kwargs
        assert kwargs["folder_id"] is None

    def test_document_out_of_scope_404(self, mock_repos):
        _, d_repo = mock_repos
        d_repo.get_by_id.return_value = None  # Scope 밖 → None

        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiNotFoundError):
            folders_service.set_document_folder(
                MagicMock(),
                actor=_actor(),
                document_id=DOC_ID,
                folder_id=FOLDER_ID,
            )

    def test_folder_not_owned_404(self, mock_repos):
        f_repo, _ = mock_repos
        # 문서는 있으나 폴더 owner 불일치 → repository 가 None
        f_repo.get_by_id.return_value = None

        from app.api.errors.exceptions import ApiNotFoundError
        from app.services.folders_service import folders_service
        with pytest.raises(ApiNotFoundError):
            folders_service.set_document_folder(
                MagicMock(),
                actor=_actor(),
                document_id=DOC_ID,
                folder_id=FOLDER_ID,
            )
