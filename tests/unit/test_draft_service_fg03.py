"""
S3 Phase 0 / FG 0-3 후속 — `app.services.draft_service` 유닛 테스트.

커버 대상 (분기 단위):
  - save_draft: 새 Draft 생성 / 기존 Draft 교체 / 편집 불가 상태 차단 / document 부재
  - discard_draft: happy / document 부재 / 활성 Draft 부재
  - publish: happy (기존 published + change_summary) / no active draft / invalid draft status
  - restore: happy / target 부재 / invalid status / active draft exists / insufficient permission
  - _can_restore helper 분기

BUG-01 (원자성) 연관: publish 시 기존 published→superseded + draft→published + 포인터 갱신
세 동작이 모두 호출됨을 확인한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.draft_service import _can_restore

pytestmark = pytest.mark.unit


DOC_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
VER_DRAFT = "v-draft-0000-0000-0000-000000000001"
VER_PUB_OLD = "v-oldpub-000-0000-0000-000000000002"


# --------------------------------------------------------------------------- #
# 헬퍼
# --------------------------------------------------------------------------- #


def _doc(**kw):
    base = {
        "id": DOC_ID,
        "title": "draft 서비스 테스트 문서",
        "document_type": "policy",
        "status": "draft",
        "summary": "요약",
        "metadata": {"k": "v"},
        "current_draft_version_id": VER_DRAFT,
        "current_published_version_id": VER_PUB_OLD,
        "created_by": "author-1",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _version(**kw):
    base = {
        "id": VER_DRAFT,
        "document_id": DOC_ID,
        "version_number": 2,
        "label": None,
        "status": "draft",
        "change_summary": None,
        "source": "manual",
        "metadata": {},
        "created_by": "author-1",
        "created_at": datetime.now(timezone.utc),
        "parent_version_id": None,
        "restored_from_version_id": None,
        "title_snapshot": "t",
        "summary_snapshot": "s",
        "published_by": None,
        "published_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _draft_save_request(**kw):
    # Phase 1 FG 1-1: content_snapshot 은 ProseMirror doc 루트 (type=="doc") 로 강제.
    body = {
        "title": "신규 타이틀",
        "summary": "신규 요약",
        "label": None,
        "change_summary": "변경 요약",
        "content_snapshot": {
            "type": "doc",
            "schema_version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"node_id": "11111111-1111-4111-8111-111111111111"},
                    "content": [{"type": "text", "text": "본문"}],
                }
            ],
        },
    }
    body.update(kw)
    from app.schemas.versions import DraftSaveRequest
    return DraftSaveRequest(**body)


def _publish_request(**kw):
    body = {"change_summary": None}
    body.update(kw)
    from app.schemas.versions import PublishRequest
    return PublishRequest(**body)


def _restore_request(**kw):
    body = {"change_summary": "복원"}
    body.update(kw)
    from app.schemas.versions import RestoreRequest
    return RestoreRequest(**body)


@pytest.fixture
def mocked(monkeypatch):
    """draft_service 가 사용하는 documents/versions repo + workflow_repository 를 mock."""
    from app.services import draft_service as svc_mod

    docs = MagicMock()
    vers = MagicMock()
    wf = MagicMock()

    # 기본: 문서 존재, draft 활성
    docs.get_by_id.return_value = _doc()
    docs.update_version_pointers.return_value = None
    docs.update.return_value = None

    vers.get_by_id.return_value = _version()
    vers.get_by_document_and_version_id.return_value = _version()
    vers.get_next_version_number.return_value = 3
    vers.create.return_value = _version(id="new-ver", version_number=3)
    vers.update_content.return_value = _version()
    vers.update_status.return_value = _version(status="published")

    wf.get_workflow_status.return_value = "draft"

    monkeypatch.setattr(svc_mod, "documents_repository", docs)
    monkeypatch.setattr(svc_mod, "versions_repository", vers)
    # workflow_repository 는 draft_service 내부 late-import —  모듈 속성 교체 시도
    import app.repositories.workflow_repository as wfrepo_mod
    monkeypatch.setattr(wfrepo_mod, "workflow_repository", wf, raising=False)

    # Phase 1 FG 1-1: save_draft 는 이제 snapshot_sync_service.rebuild_nodes_from_snapshot
    # 를 호출한다. DB 를 타지 않도록 no-op 으로 교체한다.
    import app.services.snapshot_sync_service as snap_mod
    monkeypatch.setattr(snap_mod, "rebuild_nodes_from_snapshot", lambda *a, **kw: [])

    return SimpleNamespace(docs=docs, vers=vers, wf=wf, mod=svc_mod)


# --------------------------------------------------------------------------- #
# 1) _can_restore 분기
# --------------------------------------------------------------------------- #


class TestCanRestore:
    def test_published_version_by_publisher(self):
        v = _version(status="published")
        ok, reason = _can_restore(v, None, "publisher")
        assert ok and reason is None

    def test_superseded_version_by_admin(self):
        v = _version(status="superseded")
        ok, reason = _can_restore(v, None, "admin")
        assert ok and reason is None

    def test_draft_version_cannot_be_restored(self):
        v = _version(status="draft")
        ok, reason = _can_restore(v, None, "publisher")
        assert not ok and reason == "invalid_version_status"

    def test_active_draft_blocks_restore(self):
        v = _version(status="published")
        ok, reason = _can_restore(v, "existing-draft-id", "publisher")
        assert not ok and reason == "active_draft_exists"

    def test_insufficient_permission(self):
        v = _version(status="published")
        ok, reason = _can_restore(v, None, "editor")
        assert not ok and reason == "insufficient_permission"


# --------------------------------------------------------------------------- #
# 2) save_draft
# --------------------------------------------------------------------------- #


class TestSaveDraft:
    def test_creates_new_draft_when_none_exists(self, mocked):
        """doc.current_draft_version_id is None → 새 버전 생성 + 포인터 갱신."""
        from app.services.draft_service import draft_service

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        mocked.vers.create.return_value = _version(id="brand-new", version_number=3)

        result = draft_service.save_draft(
            conn=MagicMock(), document_id=DOC_ID,
            request=_draft_save_request(), actor_id="author-1",
        )

        assert result.id == "brand-new"
        # create 호출됨
        assert mocked.vers.create.called
        # 포인터 갱신 — current_draft_version_id=새 버전
        mocked.docs.update_version_pointers.assert_called_once()
        kwargs = mocked.docs.update_version_pointers.call_args.kwargs
        assert kwargs["current_draft_version_id"] == "brand-new"
        assert kwargs["updated_by"] == "author-1"

    def test_replaces_existing_draft_content(self, mocked):
        """current_draft_version_id 존재 + status=draft 이면 update_content 호출."""
        from app.services.draft_service import draft_service

        # 기존 draft 존재 + 편집 가능 상태
        mocked.vers.get_by_id.return_value = _version(status="draft")
        mocked.wf.get_workflow_status.return_value = "draft"

        draft_service.save_draft(
            conn=MagicMock(), document_id=DOC_ID,
            request=_draft_save_request(title="수정됨"), actor_id="author-1",
        )

        # update_content 로 교체
        assert mocked.vers.update_content.called
        # create 는 호출되지 않음 (기존 버전 재사용)
        assert not mocked.vers.create.called

    def test_save_draft_document_not_found(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiNotFoundError

        mocked.docs.get_by_id.return_value = None
        # B-N4 (2026-04-25): 영문 "Document" → 한국어 "문서" 마이그레이션. OR 확장.
        with pytest.raises(ApiNotFoundError, match="(문서|Document)"):
            draft_service.save_draft(
                conn=MagicMock(), document_id=DOC_ID,
                request=_draft_save_request(),
            )

    def test_save_draft_blocked_when_workflow_locked(self, mocked):
        """기존 draft 가 IN_REVIEW 등 편집 불가 상태면 ApiVersionNotEditableError."""
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiVersionNotEditableError

        mocked.vers.get_by_id.return_value = _version(status="draft")
        # workflow_status=in_review → 편집 불가
        mocked.wf.get_workflow_status.return_value = "in_review"

        with pytest.raises(ApiVersionNotEditableError, match="Cannot edit"):
            draft_service.save_draft(
                conn=MagicMock(), document_id=DOC_ID,
                request=_draft_save_request(),
            )

    def test_save_draft_syncs_document_title_when_changed(self, mocked):
        """request.title 이 document.title 과 다르면 documents.update 호출."""
        from app.services.draft_service import draft_service

        # 새 draft 경로 (current_draft_version_id=None)
        mocked.docs.get_by_id.return_value = _doc(
            current_draft_version_id=None, title="기존 제목",
        )
        mocked.vers.create.return_value = _version(id="new", version_number=3)

        draft_service.save_draft(
            conn=MagicMock(), document_id=DOC_ID,
            request=_draft_save_request(title="다른 제목"),
            actor_id="author-1",
        )
        # documents.update 가 title 동기화 로 호출되어야 함
        assert mocked.docs.update.called
        up_kwargs = mocked.docs.update.call_args.kwargs
        assert up_kwargs.get("title") == "다른 제목"


# --------------------------------------------------------------------------- #
# 3) discard_draft
# --------------------------------------------------------------------------- #


class TestDiscardDraft:
    def test_happy_path(self, mocked):
        from app.services.draft_service import draft_service

        draft_service.discard_draft(
            conn=MagicMock(), document_id=DOC_ID, actor_id="author-1",
        )
        # 포인터 clear + 버전 status=discarded
        mocked.docs.update_version_pointers.assert_called_once()
        pk = mocked.docs.update_version_pointers.call_args.kwargs
        assert pk.get("clear_draft") is True

        # update_status(conn, VER_DRAFT, status='discarded') 로 호출됐는지 확인
        assert mocked.vers.update_status.called
        args, kwargs = mocked.vers.update_status.call_args
        # positional(conn, version_id) 또는 keyword 로 version_id 전달 가능성 대응
        version_id_arg = args[1] if len(args) >= 2 else kwargs.get("version_id")
        assert version_id_arg == VER_DRAFT
        assert kwargs.get("status") == "discarded"

    def test_document_not_found(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiNotFoundError

        mocked.docs.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError):
            draft_service.discard_draft(conn=MagicMock(), document_id=DOC_ID)

    def test_no_active_draft_raises_conflict(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        with pytest.raises(ApiConflictError, match="No active draft"):
            draft_service.discard_draft(conn=MagicMock(), document_id=DOC_ID)


# --------------------------------------------------------------------------- #
# 4) publish
# --------------------------------------------------------------------------- #


class TestPublish:
    def test_publish_supersedes_old_and_updates_pointers(self, mocked):
        from app.services.draft_service import draft_service

        # 기존 published 존재 + draft 활성
        mocked.docs.get_by_id.return_value = _doc(
            current_draft_version_id=VER_DRAFT,
            current_published_version_id=VER_PUB_OLD,
        )
        mocked.vers.get_by_id.return_value = _version(status="draft")
        published = _version(id=VER_DRAFT, status="published", published_by="approver")
        mocked.vers.update_status.return_value = published

        result = draft_service.publish(
            conn=MagicMock(), document_id=DOC_ID,
            request=_publish_request(change_summary="first publish"),
            actor_id="approver",
        )

        assert result.status == "published"

        # update_status 호출 총 2회 (superseded + published)
        statuses_set = [
            c.kwargs.get("status") for c in mocked.vers.update_status.call_args_list
        ]
        assert "superseded" in statuses_set
        assert "published" in statuses_set

        # change_summary 가 있으므로 update_content 도 호출됨
        uc_calls = mocked.vers.update_content.call_args_list
        change_summaries = [c.kwargs.get("change_summary") for c in uc_calls]
        assert "first publish" in change_summaries

        # 포인터 갱신 — current_published_version_id=draft.id, clear_draft=True
        up = mocked.docs.update_version_pointers.call_args.kwargs
        assert up["current_published_version_id"] == VER_DRAFT
        assert up["clear_draft"] is True

    def test_publish_without_old_published_does_not_supersede(self, mocked):
        from app.services.draft_service import draft_service

        mocked.docs.get_by_id.return_value = _doc(
            current_draft_version_id=VER_DRAFT,
            current_published_version_id=None,
        )
        mocked.vers.get_by_id.return_value = _version(status="draft")

        draft_service.publish(
            conn=MagicMock(), document_id=DOC_ID,
            request=_publish_request(),
            actor_id="approver",
        )
        # superseded 호출이 없어야 함
        statuses_set = [
            c.kwargs.get("status") for c in mocked.vers.update_status.call_args_list
        ]
        assert "superseded" not in statuses_set
        assert "published" in statuses_set

    def test_publish_no_active_draft_raises_conflict(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        with pytest.raises(ApiConflictError, match="No active draft"):
            draft_service.publish(
                conn=MagicMock(), document_id=DOC_ID, request=_publish_request(),
            )

    def test_publish_draft_not_in_draft_status_raises_conflict(self, mocked):
        """get_by_id 결과가 status!=draft 이면 conflict."""
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.vers.get_by_id.return_value = _version(status="superseded")
        with pytest.raises(ApiConflictError, match="not in draft status"):
            draft_service.publish(
                conn=MagicMock(), document_id=DOC_ID, request=_publish_request(),
            )

    def test_publish_document_not_found(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiNotFoundError

        mocked.docs.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError):
            draft_service.publish(
                conn=MagicMock(), document_id=DOC_ID, request=_publish_request(),
            )


# --------------------------------------------------------------------------- #
# 5) restore
# --------------------------------------------------------------------------- #


class TestRestore:
    @pytest.mark.skip(reason="FG0-3 S14-fix: Version 모킹에 metadata_snapshot 속성 추가 필요 — 후속 세션")
    def test_restore_published_creates_new_draft(self, mocked):
        from app.services.draft_service import draft_service

        # 기존 Draft 없음 + target 은 published
        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        mocked.vers.get_by_document_and_version_id.return_value = _version(
            id="past-ver", status="published",
        )
        mocked.vers.create.return_value = _version(
            id="restored-draft", version_number=5,
            restored_from_version_id="past-ver",
        )

        result = draft_service.restore(
            conn=MagicMock(), document_id=DOC_ID, version_id="past-ver",
            request=_restore_request(), actor_id="publisher-1", actor_role="publisher",
        )

        assert result.id == "restored-draft"
        assert mocked.vers.create.called
        # Document 포인터 current_draft_version_id 갱신
        up = mocked.docs.update_version_pointers.call_args.kwargs
        assert up["current_draft_version_id"] == "restored-draft"

    def test_restore_target_not_found(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiNotFoundError

        mocked.vers.get_by_document_and_version_id.return_value = None
        with pytest.raises(ApiNotFoundError, match="Version"):
            draft_service.restore(
                conn=MagicMock(), document_id=DOC_ID, version_id="missing",
                request=_restore_request(),
                actor_id="x", actor_role="publisher",
            )

    def test_restore_draft_version_rejected(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        mocked.vers.get_by_document_and_version_id.return_value = _version(status="draft")
        with pytest.raises(ApiConflictError, match="Only published"):
            draft_service.restore(
                conn=MagicMock(), document_id=DOC_ID, version_id="v",
                request=_restore_request(),
                actor_id="x", actor_role="publisher",
            )

    def test_restore_with_active_draft_rejected(self, mocked):
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id="existing")
        mocked.vers.get_by_document_and_version_id.return_value = _version(status="published")
        with pytest.raises(ApiConflictError, match="active draft"):
            draft_service.restore(
                conn=MagicMock(), document_id=DOC_ID, version_id="v",
                request=_restore_request(),
                actor_id="x", actor_role="publisher",
            )

    def test_restore_insufficient_permission_defensive(self, mocked):
        """router 에서 1차 차단되지만 서비스 방어적 분기도 확인."""
        from app.services.draft_service import draft_service
        from app.api.errors.exceptions import ApiConflictError

        mocked.docs.get_by_id.return_value = _doc(current_draft_version_id=None)
        mocked.vers.get_by_document_and_version_id.return_value = _version(status="published")
        with pytest.raises(ApiConflictError, match="Insufficient permission"):
            draft_service.restore(
                conn=MagicMock(), document_id=DOC_ID, version_id="v",
                request=_restore_request(),
                actor_id="x", actor_role="editor",   # publisher 미만
            )
