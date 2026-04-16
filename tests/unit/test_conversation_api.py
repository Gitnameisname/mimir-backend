"""
Conversations API 단위 테스트 — Task 3-2.

테스트 범위:
  - Pydantic 스키마 유효성 검사 (ConversationCreateRequest, ConversationUpdateRequest, RedactRequest)
  - API 라우터 핵심 로직 (get/post/put/delete, turns, redact)
  - ACL 헬퍼 (_assert_read_access, _assert_write_access)
  - actor_type 감사 로그 문자열 변환 (_actor_type_str)
  - Scope Profile 해석 (_resolve_scope_profile)
  - router.py 등록 여부 (정적 검사)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


# ---------------------------------------------------------------------------
# 픽스처 헬퍼
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_conv(
    owner_id: str | None = None,
    access_level: str = "private",
    status: str = "active",
):
    """도메인 Conversation 객체 생성 헬퍼."""
    from app.models.conversation import Conversation

    oid = owner_id or str(uuid4())
    now = _now()
    return Conversation(
        id=str(uuid4()),
        owner_id=oid,
        organization_id=str(uuid4()),
        title="테스트 대화",
        status=status,
        metadata={},
        retention_days=90,
        access_level=access_level,
        created_at=now,
        updated_at=now,
        expires_at=None,
        deleted_at=None,
    )


def _make_turn(conversation_id: str | None = None, turn_number: int = 1):
    from app.models.conversation import Turn

    now = _now()
    return Turn(
        id=str(uuid4()),
        conversation_id=conversation_id or str(uuid4()),
        turn_number=turn_number,
        user_message="질문",
        assistant_response="응답",
        retrieval_metadata={},
        created_at=now,
    )


def _make_message(turn_id: str | None = None, role: str = "user"):
    from app.models.conversation import Message

    now = _now()
    return Message(
        id=str(uuid4()),
        turn_id=turn_id or str(uuid4()),
        role=role,
        content="내용",
        metadata={},
        created_at=now,
    )


def _make_actor(actor_id: str | None = None, role: str = "VIEWER", actor_type_val: str = "user"):
    from app.api.auth.models import ActorContext, ActorType

    return ActorContext(
        actor_type=ActorType.USER if actor_type_val == "user" else ActorType.SERVICE,
        actor_id=actor_id or str(uuid4()),
        is_authenticated=True,
        auth_method=None,
        tenant_id=str(uuid4()),
        role=role,
    )


def _make_request_ctx():
    """request.state.context 모의 객체."""
    ctx = MagicMock()
    ctx.request_id = str(uuid4())
    request = MagicMock()
    request.state.context = ctx
    return request


# ===========================================================================
# 1. Pydantic 스키마 테스트
# ===========================================================================

class TestConversationCreateRequest:
    def test_valid_minimal(self):
        from app.schemas.conversation import ConversationCreateRequest

        req = ConversationCreateRequest(title="새 대화")
        assert req.title == "새 대화"
        assert req.access_level == "private"
        assert req.retention_days is None

    def test_valid_full(self):
        from app.schemas.conversation import ConversationCreateRequest

        req = ConversationCreateRequest(
            title="조직 공유 대화",
            metadata={"project": "mimir"},
            retention_days=180,
            access_level="organization",
        )
        assert req.retention_days == 180
        assert req.access_level == "organization"

    def test_invalid_empty_title(self):
        from app.schemas.conversation import ConversationCreateRequest

        with pytest.raises(ValidationError):
            ConversationCreateRequest(title="")

    def test_invalid_access_level(self):
        from app.schemas.conversation import ConversationCreateRequest

        with pytest.raises(ValidationError):
            ConversationCreateRequest(title="제목", access_level="team")

    def test_invalid_retention_days_zero(self):
        from app.schemas.conversation import ConversationCreateRequest

        with pytest.raises(ValidationError):
            ConversationCreateRequest(title="제목", retention_days=0)

    def test_invalid_retention_days_too_large(self):
        from app.schemas.conversation import ConversationCreateRequest

        with pytest.raises(ValidationError):
            ConversationCreateRequest(title="제목", retention_days=9999)

    def test_title_max_length(self):
        from app.schemas.conversation import ConversationCreateRequest

        with pytest.raises(ValidationError):
            ConversationCreateRequest(title="A" * 257)

    def test_access_level_public_allowed(self):
        from app.schemas.conversation import ConversationCreateRequest

        req = ConversationCreateRequest(title="공개", access_level="public")
        assert req.access_level == "public"


class TestConversationUpdateRequest:
    def test_all_none_is_valid(self):
        from app.schemas.conversation import ConversationUpdateRequest

        req = ConversationUpdateRequest()
        assert req.title is None
        assert req.status is None

    def test_valid_status_archived(self):
        from app.schemas.conversation import ConversationUpdateRequest

        req = ConversationUpdateRequest(status="archived")
        assert req.status == "archived"

    def test_invalid_status(self):
        from app.schemas.conversation import ConversationUpdateRequest

        with pytest.raises(ValidationError):
            ConversationUpdateRequest(status="deleted")

    def test_invalid_access_level(self):
        from app.schemas.conversation import ConversationUpdateRequest

        with pytest.raises(ValidationError):
            ConversationUpdateRequest(access_level="internal")


class TestRedactRequest:
    def test_valid_user_message(self):
        from app.schemas.conversation import RedactRequest

        req = RedactRequest(fields=["user_message"], reason="PII 포함")
        assert req.fields == ["user_message"]

    def test_valid_both_fields(self):
        from app.schemas.conversation import RedactRequest

        req = RedactRequest(
            fields=["user_message", "assistant_response"],
            reason="민감 데이터",
        )
        assert len(req.fields) == 2

    def test_invalid_field_name(self):
        from app.schemas.conversation import RedactRequest

        with pytest.raises(ValidationError):
            RedactRequest(fields=["content"], reason="이유")

    def test_empty_fields_list(self):
        from app.schemas.conversation import RedactRequest

        with pytest.raises(ValidationError):
            RedactRequest(fields=[], reason="이유")

    def test_empty_reason_rejected(self):
        from app.schemas.conversation import RedactRequest

        with pytest.raises(ValidationError):
            RedactRequest(fields=["user_message"], reason="")


# ===========================================================================
# 2. ACL 헬퍼 테스트
# ===========================================================================

class TestAssertReadAccess:
    def test_owner_can_read(self):
        from app.api.v1.conversations import _assert_read_access

        actor = _make_actor()
        conv = _make_conv(owner_id=str(actor.actor_id), access_level="private")
        # 예외 없이 통과해야 함
        _assert_read_access(conv, actor)

    def test_organization_access_level_allows_others(self):
        from app.api.v1.conversations import _assert_read_access

        actor = _make_actor()
        conv = _make_conv(owner_id=str(uuid4()), access_level="organization")
        _assert_read_access(conv, actor)

    def test_public_allows_anyone(self):
        from app.api.v1.conversations import _assert_read_access

        actor = _make_actor()
        conv = _make_conv(owner_id=str(uuid4()), access_level="public")
        _assert_read_access(conv, actor)

    def test_private_non_owner_raises_403(self):
        from app.api.v1.conversations import _assert_read_access

        actor = _make_actor()
        conv = _make_conv(owner_id=str(uuid4()), access_level="private")
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_access(conv, actor)
        assert exc_info.value.status_code == 403


class TestAssertWriteAccess:
    def test_owner_can_write(self):
        from app.api.v1.conversations import _assert_write_access

        actor = _make_actor()
        conv = _make_conv(owner_id=str(actor.actor_id))
        _assert_write_access(conv, actor)

    def test_admin_can_write(self):
        from app.api.v1.conversations import _assert_write_access

        actor = _make_actor(role="SUPER_ADMIN")
        conv = _make_conv(owner_id=str(uuid4()))
        _assert_write_access(conv, actor)

    def test_org_admin_can_write(self):
        from app.api.v1.conversations import _assert_write_access

        actor = _make_actor(role="ORG_ADMIN")
        conv = _make_conv(owner_id=str(uuid4()))
        _assert_write_access(conv, actor)

    def test_non_owner_non_admin_raises_403(self):
        from app.api.v1.conversations import _assert_write_access

        actor = _make_actor(role="VIEWER")
        conv = _make_conv(owner_id=str(uuid4()))
        with pytest.raises(HTTPException) as exc_info:
            _assert_write_access(conv, actor)
        assert exc_info.value.status_code == 403


# ===========================================================================
# 3. actor_type 변환 테스트
# ===========================================================================

class TestActorTypeStr:
    def test_user_actor_returns_user(self):
        from app.api.v1.conversations import _actor_type_str

        actor = _make_actor(actor_type_val="user")
        assert _actor_type_str(actor) == "user"

    def test_service_actor_returns_agent(self):
        from app.api.auth.models import ActorContext, ActorType
        from app.api.v1.conversations import _actor_type_str

        actor = ActorContext(
            actor_type=ActorType.SERVICE,
            actor_id="svc-001",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        assert _actor_type_str(actor) == "agent"


# ===========================================================================
# 4. Scope Profile 테스트
# ===========================================================================

class TestResolveScopeProfile:
    def test_super_admin_gets_organization_scope(self):
        from app.api.v1.conversations import _resolve_scope_profile

        actor = _make_actor(role="SUPER_ADMIN")
        profile = _resolve_scope_profile(actor)
        assert profile["scope"] == "organization"
        assert profile["include_archived"] is True

    def test_org_admin_gets_organization_scope(self):
        from app.api.v1.conversations import _resolve_scope_profile

        actor = _make_actor(role="ORG_ADMIN")
        profile = _resolve_scope_profile(actor)
        assert profile["scope"] == "organization"

    def test_service_actor_gets_organization_scope(self):
        from app.api.auth.models import ActorContext, ActorType
        from app.api.v1.conversations import _resolve_scope_profile

        actor = ActorContext(
            actor_type=ActorType.SERVICE,
            actor_id="svc-001",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        profile = _resolve_scope_profile(actor)
        assert profile["scope"] == "organization"

    def test_regular_user_gets_private_scope(self):
        from app.api.v1.conversations import _resolve_scope_profile

        actor = _make_actor(role="VIEWER")
        profile = _resolve_scope_profile(actor)
        assert profile["scope"] == "private"
        assert profile["include_archived"] is False


# ===========================================================================
# 5. 라우터 함수 단위 테스트 (DB mock)
# ===========================================================================

class TestCreateConversation:
    def test_create_returns_201_data(self):
        from app.api.v1.conversations import create_conversation
        from app.schemas.conversation import ConversationCreateRequest

        actor = _make_actor()
        request = _make_request_ctx()
        body = ConversationCreateRequest(title="새 대화")

        conv = _make_conv(owner_id=str(actor.actor_id))

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.create.return_value = conv

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo), \
             patch("app.api.v1.conversations.audit_emitter"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = create_conversation(request=request, body=body, actor=actor)

        assert result.data.title == conv.title
        mock_repo.create.assert_called_once()

    def test_create_uses_default_retention_days(self):
        from app.api.v1.conversations import create_conversation
        from app.schemas.conversation import ConversationCreateRequest

        actor = _make_actor()
        request = _make_request_ctx()
        body = ConversationCreateRequest(title="기본 보존")  # retention_days 미입력

        conv = _make_conv(owner_id=str(actor.actor_id))
        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.create.return_value = conv

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo), \
             patch("app.api.v1.conversations.audit_emitter"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            create_conversation(request=request, body=body, actor=actor)

        call_kwargs = mock_repo.create.call_args[1]
        assert call_kwargs["retention_days"] == 90


class TestGetConversation:
    def test_get_existing_conversation(self):
        from app.api.v1.conversations import get_conversation

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.list_by_conversation.return_value = []
        mock_msg_repo = MagicMock()

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.MessageRepository", return_value=mock_msg_repo):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = get_conversation(
                request=request,
                conversation_id=conv.id,
                include_turns=True,
                actor=actor,
            )

        assert result.data.id == conv.id

    def test_get_nonexistent_raises_404(self):
        from app.api.v1.conversations import get_conversation

        actor = _make_actor()
        request = _make_request_ctx()

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = None

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository"), \
             patch("app.api.v1.conversations.MessageRepository"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                get_conversation(
                    request=request,
                    conversation_id=str(uuid4()),
                    include_turns=False,
                    actor=actor,
                )
        assert exc_info.value.status_code == 404

    def test_get_private_conv_by_non_owner_raises_403(self):
        from app.api.v1.conversations import get_conversation

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(uuid4()), access_level="private")

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository"), \
             patch("app.api.v1.conversations.MessageRepository"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                get_conversation(
                    request=request,
                    conversation_id=conv.id,
                    include_turns=False,
                    actor=actor,
                )
        assert exc_info.value.status_code == 403


class TestUpdateConversation:
    def test_update_title_succeeds(self):
        from app.api.v1.conversations import update_conversation
        from app.schemas.conversation import ConversationUpdateRequest

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))
        updated_conv = _make_conv(owner_id=str(actor.actor_id))
        updated_conv.title = "수정된 제목"

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_by_id.return_value = conv
        mock_repo.update.return_value = updated_conv

        body = ConversationUpdateRequest(title="수정된 제목")

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo), \
             patch("app.api.v1.conversations.audit_emitter"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = update_conversation(
                request=request,
                conversation_id=conv.id,
                body=body,
                actor=actor,
            )

        assert result.data.title == "수정된 제목"

    def test_update_by_non_owner_raises_403(self):
        from app.api.v1.conversations import update_conversation
        from app.schemas.conversation import ConversationUpdateRequest

        actor = _make_actor(role="VIEWER")
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(uuid4()))

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_by_id.return_value = conv

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                update_conversation(
                    request=request,
                    conversation_id=conv.id,
                    body=ConversationUpdateRequest(title="시도"),
                    actor=actor,
                )
        assert exc_info.value.status_code == 403


class TestDeleteConversation:
    def test_soft_delete_succeeds(self):
        from app.api.v1.conversations import delete_conversation

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_by_id.return_value = conv
        mock_repo.soft_delete.return_value = True

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo), \
             patch("app.api.v1.conversations.audit_emitter"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = delete_conversation(
                request=request,
                conversation_id=conv.id,
                actor=actor,
            )

        assert result.data["deleted"] is True

    def test_already_deleted_raises_409(self):
        from app.api.v1.conversations import delete_conversation

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_by_id.return_value = conv
        mock_repo.soft_delete.return_value = False  # already deleted

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_repo), \
             patch("app.api.v1.conversations.audit_emitter"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                delete_conversation(
                    request=request,
                    conversation_id=conv.id,
                    actor=actor,
                )
        assert exc_info.value.status_code == 409


class TestListTurns:
    def test_list_turns_returns_all_turns(self):
        from app.api.v1.conversations import list_turns

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))
        turns = [_make_turn(conversation_id=conv.id, turn_number=i) for i in range(1, 4)]

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.list_by_conversation.return_value = turns
        mock_msg_repo = MagicMock()
        mock_msg_repo.list_by_turn.return_value = []

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.MessageRepository", return_value=mock_msg_repo):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = list_turns(request=request, conversation_id=conv.id, actor=actor)

        assert len(result.data) == 3


class TestGetTurn:
    def test_get_turn_returns_turn_with_messages(self):
        from app.api.v1.conversations import get_turn

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))
        turn = _make_turn(conversation_id=conv.id)
        messages = [_make_message(turn_id=turn.id, role=r) for r in ("user", "assistant")]

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.get_by_id.return_value = turn
        mock_msg_repo = MagicMock()
        mock_msg_repo.list_by_turn.return_value = messages

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.MessageRepository", return_value=mock_msg_repo):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = get_turn(
                request=request,
                conversation_id=conv.id,
                turn_id=turn.id,
                actor=actor,
            )

        assert result.data.id == turn.id
        assert len(result.data.messages) == 2

    def test_get_turn_wrong_conversation_raises_404(self):
        from app.api.v1.conversations import get_turn

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))
        # turn belongs to a different conversation
        turn = _make_turn(conversation_id=str(uuid4()))

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.get_by_id.return_value = turn

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.MessageRepository"):
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(HTTPException) as exc_info:
                get_turn(
                    request=request,
                    conversation_id=conv.id,
                    turn_id=turn.id,
                    actor=actor,
                )
        assert exc_info.value.status_code == 404


class TestRedactTurn:
    def test_redact_succeeds(self):
        from app.api.v1.conversations import redact_turn
        from app.schemas.conversation import RedactRequest

        actor = _make_actor()
        request = _make_request_ctx()
        conv = _make_conv(owner_id=str(actor.actor_id))
        turn = _make_turn(conversation_id=conv.id)
        body = RedactRequest(fields=["user_message"], reason="PII 제거")

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.get_by_id.return_value = turn
        mock_turn_repo.redact_turn.return_value = True

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.audit_emitter") as mock_audit:
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            result = redact_turn(
                request=request,
                conversation_id=conv.id,
                turn_id=turn.id,
                body=body,
                actor=actor,
            )

        assert result.data["redacted"] is True
        assert result.data["fields"] == ["user_message"]
        # 감사 로그 호출 확인
        mock_audit.emit.assert_called_once()
        emit_kwargs = mock_audit.emit.call_args[1]
        assert emit_kwargs["event_type"] == "turn.redacted"
        assert emit_kwargs["actor_type"] == "user"
        assert "reason" in emit_kwargs["metadata"]

    def test_redact_emits_agent_actor_type_for_service(self):
        from app.api.auth.models import ActorContext, ActorType
        from app.api.v1.conversations import redact_turn
        from app.schemas.conversation import RedactRequest

        actor = ActorContext(
            actor_type=ActorType.SERVICE,
            actor_id="svc-001",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        request = _make_request_ctx()
        conv = _make_conv(owner_id="svc-001")
        turn = _make_turn(conversation_id=conv.id)
        body = RedactRequest(fields=["assistant_response"], reason="에이전트 호출")

        mock_conn = MagicMock()
        mock_conv_repo = MagicMock()
        mock_conv_repo.get_by_id.return_value = conv
        mock_turn_repo = MagicMock()
        mock_turn_repo.get_by_id.return_value = turn
        mock_turn_repo.redact_turn.return_value = True

        with patch("app.api.v1.conversations.get_db") as mock_get_db, \
             patch("app.api.v1.conversations.ConversationRepository", return_value=mock_conv_repo), \
             patch("app.api.v1.conversations.TurnRepository", return_value=mock_turn_repo), \
             patch("app.api.v1.conversations.audit_emitter") as mock_audit:
            mock_get_db.return_value.__enter__ = lambda s: mock_conn
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            redact_turn(
                request=request,
                conversation_id=conv.id,
                turn_id=turn.id,
                body=body,
                actor=actor,
            )

        emit_kwargs = mock_audit.emit.call_args[1]
        assert emit_kwargs["actor_type"] == "agent"


# ===========================================================================
# 6. 라우터 등록 정적 검사
# ===========================================================================

class TestRouterRegistration:
    def test_conversations_imported_in_router(self):
        ROUTER_PY = (ROOT / "backend/app/api/v1/router.py").read_text(encoding="utf-8")
        assert "from app.api.v1 import conversations" in ROUTER_PY

    def test_conversations_prefix_registered(self):
        ROUTER_PY = (ROOT / "backend/app/api/v1/router.py").read_text(encoding="utf-8")
        assert 'prefix="/conversations"' in ROUTER_PY

    def test_conversations_tag_registered(self):
        ROUTER_PY = (ROOT / "backend/app/api/v1/router.py").read_text(encoding="utf-8")
        assert '"conversations"' in ROUTER_PY
