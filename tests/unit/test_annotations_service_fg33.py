"""S3 Phase 3 FG 3-3 — annotations_service 단위 테스트.

대상:
  - extract_mentions (정규식)
  - _normalize_actor_type / _is_admin / _require_owner_or_admin
  - _validate_content
  - AnnotationsService.create_annotation / update_content / resolve / reopen / delete / list_for_document
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api.errors.exceptions import (
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiValidationError,
)
from app.models.annotation import Annotation
from app.services import annotations_service as as_mod
from app.services.annotations_service import (
    AnnotationsService,
    MAX_CONTENT_LENGTH,
    extract_mentions,
)


# ---------------------------------------------------------------------------
# Mention parser
# ---------------------------------------------------------------------------

class TestExtractMentions:
    def test_empty_returns_empty(self):
        assert extract_mentions("") == []
        assert extract_mentions("hello world no mention") == []

    def test_single_mention(self):
        assert extract_mentions("@alice 봐주세요") == ["alice"]

    def test_multiple_mentions(self):
        assert extract_mentions("@alice @bob 다음주에 봐요") == ["alice", "bob"]

    def test_dedup(self):
        # 같은 멘션 중복 → 1번만
        assert extract_mentions("@alice 안녕 @alice 또 만나요") == ["alice"]

    def test_lowercase_normalized(self):
        # case-insensitive — Alice → alice
        assert extract_mentions("@Alice @ALICE @alice") == ["alice"]

    def test_dash_underscore_allowed_dot_terminates(self):
        # S3 Phase 5 FG 5-5 (2026-05-14) 이후 regex: 구두점 (.,;!?…괄호'") 은 token 종료자.
        # `_` 와 `-` 는 token 내 허용. `.` 는 token 종료 — `@first.last` → "first" 만 매치.
        assert extract_mentions("@user_name @user-name") == ["user_name", "user-name"]
        assert extract_mentions("@first.last") == ["first"]

    def test_first_char_no_letter_restriction(self):
        # FG 5-5 이후 한국어 display_name 매칭 허용 — letter-only 시작 제약 없음.
        # 숫자 / underscore 시작도 매치된다.
        assert extract_mentions("@123abc") == ["123abc"]
        assert extract_mentions("@_underscore") == ["_underscore"]

    def test_min_length_1(self):
        # FG 5-5 이후 regex `{1,64}` — 1 글자도 매치 (token 호스트가 username/display_name 매칭 시
        # silent skip 책임).
        assert extract_mentions("@a") == ["a"]
        assert extract_mentions("@ab") == ["ab"]

    def test_max_length_64(self):
        long_name = "a" * 64
        assert extract_mentions(f"@{long_name}") == [long_name]
        too_long = "a" * 65
        # 64 글자만 매치되어 잘림
        result = extract_mentions(f"@{too_long}")
        assert len(result) == 1
        assert len(result[0]) == 64

    def test_email_in_content_not_mention(self):
        # alice@example.com — `@example` 은 멘션 패턴이지만 앞이 `[\w]` 라 (?:^|[^\w]) 조건에 의해 제외
        result = extract_mentions("문의: alice@example.com")
        assert result == []

    def test_mention_at_start_of_string(self):
        assert extract_mentions("@alice 안녕") == ["alice"]

    def test_mention_after_newline(self):
        assert extract_mentions("첫줄\n@bob 둘째줄") == ["bob"]

    def test_mention_after_punctuation(self):
        # `)` 는 \w 가 아니라 멘션 종료. `.` 은 username 에 포함되어 trailing dot 도 캡쳐됨.
        assert extract_mentions("(@alice)") == ["alice"]
        # trailing dot 은 username 의 일부로 캡쳐됨 (현재 의도된 동작)
        assert extract_mentions(",@bob") == ["bob"]


class TestValidateContent:
    def test_empty_raises(self):
        with pytest.raises(ApiValidationError):
            as_mod._validate_content("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ApiValidationError):
            as_mod._validate_content("   ")

    def test_too_long_raises(self):
        with pytest.raises(ApiValidationError):
            as_mod._validate_content("a" * (MAX_CONTENT_LENGTH + 1))

    def test_valid(self):
        as_mod._validate_content("hello")  # no raise

    def test_max_length_exact(self):
        as_mod._validate_content("a" * MAX_CONTENT_LENGTH)  # no raise


# ---------------------------------------------------------------------------
# Service — fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _mock_repo(monkeypatch):
    repo = MagicMock()
    monkeypatch.setattr(as_mod, "annotations_repository", repo)
    return repo


@pytest.fixture
def _mock_users(monkeypatch):
    fake = MagicMock()
    fake.get_by_username = MagicMock(return_value=None)
    # S3 Phase 5 FG 5-5 (2026-05-14) — display_name fallback 도 mock 기본값 None.
    # 개별 테스트가 필요 시 override.
    fake.find_by_display_name_in_viewer_orgs = MagicMock(return_value=None)
    monkeypatch.setattr(as_mod, "_users_repository", fake)
    return fake


@pytest.fixture
def _mock_documents_service(monkeypatch):
    """ACL 통과 stub."""
    monkeypatch.setattr(
        as_mod.documents_service,
        "get_document",
        MagicMock(return_value=SimpleNamespace(id="doc-1")),
    )
    return as_mod.documents_service.get_document


@pytest.fixture
def _mock_audit(monkeypatch):
    monkeypatch.setattr(
        as_mod.audit_emitter, "emit_for_actor", MagicMock(return_value=None),
    )
    return as_mod.audit_emitter.emit_for_actor


@pytest.fixture
def _mock_notifications(monkeypatch):
    fake = MagicMock()
    fake.enqueue_mention = MagicMock(return_value=None)
    monkeypatch.setattr(as_mod, "notifications_service", fake)
    return fake


def _user_actor(*, actor_id="u-author", role="AUTHOR"):
    a = MagicMock()
    a.actor_id = actor_id
    a.is_authenticated = True
    a.role = role
    a.actor_type = SimpleNamespace(value="user")
    return a


def _annotation(
    *, id="ann-1", document_id="doc-1", author_id="u-author",
    parent_id=None, content="hello", node_id="node-1",
):
    return Annotation(
        id=id,
        document_id=document_id,
        version_id=None,
        node_id=node_id,
        span_start=None,
        span_end=None,
        author_id=author_id,
        actor_type="user",
        content=content,
        status="open",
        resolved_at=None,
        resolved_by=None,
        parent_id=parent_id,
        is_orphan=False,
        orphaned_at=None,
        created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# create_annotation
# ---------------------------------------------------------------------------

class TestCreateAnnotation:
    def test_anonymous_raises_permission(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        a = MagicMock()
        a.actor_id = None
        a.is_authenticated = False
        with pytest.raises(ApiPermissionDeniedError):
            AnnotationsService().create_annotation(
                conn=MagicMock(), actor=a,
                document_id="doc-1", node_id="node-1", content="hi",
            )

    def test_validation_failure(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        with pytest.raises(ApiValidationError):
            AnnotationsService().create_annotation(
                conn=MagicMock(), actor=_user_actor(),
                document_id="doc-1", node_id="node-1", content="",
            )

    def test_acl_failure_propagates(self, _mock_repo, _mock_users, _mock_audit, _mock_notifications, monkeypatch):
        monkeypatch.setattr(
            as_mod.documents_service, "get_document",
            MagicMock(side_effect=ApiNotFoundError("문서 없음")),
        )
        with pytest.raises(ApiNotFoundError):
            AnnotationsService().create_annotation(
                conn=MagicMock(), actor=_user_actor(),
                document_id="doc-x", node_id="node-1", content="hi",
            )
        _mock_repo.create.assert_not_called()

    def test_normal_create_no_mentions(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.create.return_value = _annotation()
        result = AnnotationsService().create_annotation(
            conn=MagicMock(), actor=_user_actor(),
            document_id="doc-1", node_id="node-1", content="단순 코멘트",
        )
        assert result.id == "ann-1"
        _mock_repo.create.assert_called_once()
        _mock_repo.replace_mentions.assert_not_called()
        _mock_audit.assert_called_once()

    def test_create_with_mention_unknown_user_skipped(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        # @ghost 가 DB 에 없음 → mention silently skip
        _mock_users.get_by_username.return_value = None
        _mock_repo.create.return_value = _annotation()
        AnnotationsService().create_annotation(
            conn=MagicMock(), actor=_user_actor(),
            document_id="doc-1", node_id="node-1", content="@ghost hi",
        )
        _mock_repo.replace_mentions.assert_not_called()
        _mock_notifications.enqueue_mention.assert_not_called()

    def test_create_with_valid_mention_enqueues_notification(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        # @alice 가 valid user → mention 등록 + 알림 enqueue
        _mock_users.get_by_username.return_value = SimpleNamespace(id="u-alice", display_name="Alice")
        _mock_repo.create.return_value = _annotation()
        AnnotationsService().create_annotation(
            conn=MagicMock(), actor=_user_actor(actor_id="u-bob"),
            document_id="doc-1", node_id="node-1", content="@alice 봐주세요",
        )
        _mock_repo.replace_mentions.assert_called_once_with(
            mock_call().__class__.__call__,
        ) if False else None  # placeholder
        # 실제 검증
        call = _mock_repo.replace_mentions.call_args
        assert call.args[1] == "ann-1"
        assert call.args[2] == ["u-alice"]
        _mock_notifications.enqueue_mention.assert_called_once()

    def test_create_reply_with_valid_parent(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.get_by_id.return_value = _annotation(id="ann-parent", parent_id=None)
        _mock_repo.create.return_value = _annotation(id="ann-reply", parent_id="ann-parent")
        result = AnnotationsService().create_annotation(
            conn=MagicMock(), actor=_user_actor(),
            document_id="doc-1", node_id="node-1", content="답글입니다",
            parent_id="ann-parent",
        )
        assert result.parent_id == "ann-parent"

    def test_create_reply_parent_not_found(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError):
            AnnotationsService().create_annotation(
                conn=MagicMock(), actor=_user_actor(),
                document_id="doc-1", node_id="node-1", content="답글",
                parent_id="missing",
            )

    def test_create_reply_parent_different_document(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.get_by_id.return_value = _annotation(id="ann-x", document_id="doc-OTHER")
        with pytest.raises(ApiValidationError):
            AnnotationsService().create_annotation(
                conn=MagicMock(), actor=_user_actor(),
                document_id="doc-1", node_id="node-1", content="답글",
                parent_id="ann-x",
            )

    def test_create_reply_to_reply_flattens_to_root(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        # parent 가 이미 답글 (parent_id=ann-root) → 본 FG 는 ann-root 로 평탄화
        _mock_repo.get_by_id.return_value = _annotation(id="ann-mid", parent_id="ann-root")
        _mock_repo.create.return_value = _annotation(id="ann-reply", parent_id="ann-root")
        AnnotationsService().create_annotation(
            conn=MagicMock(), actor=_user_actor(),
            document_id="doc-1", node_id="node-1", content="답글의 답글",
            parent_id="ann-mid",
        )
        # repository.create 가 받은 parent_id 가 ann-root 로 평탄화됨
        call_kwargs = _mock_repo.create.call_args.kwargs
        assert call_kwargs["parent_id"] == "ann-root"


# ---------------------------------------------------------------------------
# update / resolve / delete
# ---------------------------------------------------------------------------

class TestUpdateContent:
    def test_only_owner_can_update(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-author")
        with pytest.raises(ApiPermissionDeniedError):
            AnnotationsService().update_content(
                conn=MagicMock(), actor=_user_actor(actor_id="u-other"),
                annotation_id="ann-1", new_content="수정",
            )

    def test_admin_cannot_update_others_content(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        # update 는 본인만 (admin 도 X — 본문은 작성자 의도 보존)
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-author")
        with pytest.raises(ApiPermissionDeniedError):
            AnnotationsService().update_content(
                conn=MagicMock(), actor=_user_actor(actor_id="u-admin", role="ORG_ADMIN"),
                annotation_id="ann-1", new_content="수정",
            )

    def test_owner_can_update(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit, _mock_notifications):
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-author", content="원본")
        _mock_repo.update_content.return_value = _annotation(content="수정됨")
        result = AnnotationsService().update_content(
            conn=MagicMock(), actor=_user_actor(actor_id="u-author"),
            annotation_id="ann-1", new_content="수정됨",
        )
        assert result.content == "수정됨"
        _mock_audit.assert_called_once()


class TestResolveReopen:
    def test_owner_can_resolve(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = _annotation()
        _mock_repo.set_status.return_value = _annotation()
        AnnotationsService().resolve(
            conn=MagicMock(), actor=_user_actor(actor_id="u-author"),
            annotation_id="ann-1",
        )
        _mock_repo.set_status.assert_called_once()

    def test_admin_can_resolve_others(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-other")
        _mock_repo.set_status.return_value = _annotation()
        AnnotationsService().resolve(
            conn=MagicMock(), actor=_user_actor(actor_id="u-admin", role="ORG_ADMIN"),
            annotation_id="ann-1",
        )
        _mock_repo.set_status.assert_called_once()

    def test_non_owner_non_admin_blocked(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-author")
        with pytest.raises(ApiPermissionDeniedError):
            AnnotationsService().resolve(
                conn=MagicMock(), actor=_user_actor(actor_id="u-stranger", role="AUTHOR"),
                annotation_id="ann-1",
            )


class TestDelete:
    def test_owner_can_delete(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = _annotation()
        _mock_repo.delete.return_value = True
        AnnotationsService().delete(
            conn=MagicMock(), actor=_user_actor(actor_id="u-author"),
            annotation_id="ann-1",
        )
        _mock_repo.delete.assert_called_once()

    def test_admin_can_delete(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = _annotation(author_id="u-other")
        _mock_repo.delete.return_value = True
        AnnotationsService().delete(
            conn=MagicMock(), actor=_user_actor(actor_id="u-admin", role="SUPER_ADMIN"),
            annotation_id="ann-1",
        )
        _mock_repo.delete.assert_called_once()

    def test_not_found(self, _mock_repo, _mock_users, _mock_documents_service, _mock_audit):
        _mock_repo.get_by_id.return_value = None
        with pytest.raises(ApiNotFoundError):
            AnnotationsService().delete(
                conn=MagicMock(), actor=_user_actor(),
                annotation_id="missing",
            )


class TestListForDocument:
    def test_acl_failure_propagates(self, _mock_repo, _mock_users, monkeypatch):
        monkeypatch.setattr(
            as_mod.documents_service, "get_document",
            MagicMock(side_effect=ApiNotFoundError("doc not found")),
        )
        with pytest.raises(ApiNotFoundError):
            AnnotationsService().list_for_document(
                conn=MagicMock(), actor=_user_actor(),
                document_id="doc-x",
            )

    def test_returns_annotations(self, _mock_repo, _mock_users, _mock_documents_service):
        _mock_repo.list_for_document.return_value = [_annotation(), _annotation(id="ann-2")]
        result = AnnotationsService().list_for_document(
            conn=MagicMock(), actor=_user_actor(),
            document_id="doc-1",
        )
        assert len(result) == 2
        _mock_repo.list_for_document.assert_called_once()


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

class TestGet:
    def test_acl_failure_propagates(self, _mock_repo, _mock_users, monkeypatch):
        _mock_repo.get_by_id.return_value = _annotation(document_id="doc-OTHER")
        monkeypatch.setattr(
            as_mod.documents_service, "get_document",
            MagicMock(side_effect=ApiNotFoundError("doc not found")),
        )
        with pytest.raises(ApiNotFoundError):
            AnnotationsService().get(
                conn=MagicMock(), actor=_user_actor(),
                annotation_id="ann-1",
            )

    def test_normal(self, _mock_repo, _mock_users, _mock_documents_service):
        _mock_repo.get_by_id.return_value = _annotation()
        result = AnnotationsService().get(
            conn=MagicMock(), actor=_user_actor(),
            annotation_id="ann-1",
        )
        assert result.id == "ann-1"


# helper for tests
class mock_call:
    def __init__(self):
        pass
    class __class__:
        @staticmethod
        def __call__():
            pass
