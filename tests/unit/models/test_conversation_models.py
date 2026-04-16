"""
Conversation 도메인 모델 및 Repository 단위 테스트 — Task 3-1.

테스트 범위:
  - Conversation / Turn / Message 도메인 모델 (dataclass)
  - ConversationRepository / TurnRepository / MessageRepository
    → psycopg2 연결을 MagicMock 으로 대체하여 DB 없이 실행
  - soft delete, expires_at 계산, redact 등 핵심 로직 검증
  - audit emitter actor_type 필드 포함 여부 검증
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from app.models.conversation import Conversation, Message, Turn
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
    TurnRepository,
    _row_to_conversation,
    _row_to_message,
    _row_to_turn,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _make_conversation_row(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": uuid4(),
        "owner_id": uuid4(),
        "organization_id": uuid4(),
        "title": "테스트 대화",
        "status": "active",
        "metadata": {},
        "retention_days": 90,
        "access_level": "private",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(days=90),
        "deleted_at": None,
    }
    base.update(overrides)
    return base


def _make_turn_row(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": uuid4(),
        "conversation_id": uuid4(),
        "turn_number": 1,
        "user_message": "AI란 무엇인가요?",
        "assistant_response": "AI는 인공지능입니다.",
        "retrieval_metadata": {"citations": [], "query_original": "AI란?"},
        "created_at": now,
    }
    base.update(overrides)
    return base


def _make_message_row(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": uuid4(),
        "turn_id": uuid4(),
        "role": "user",
        "content": "질문 내용",
        "metadata": {"token_count": 5},
        "created_at": now,
    }
    base.update(overrides)
    return base


def _make_mock_conn(fetchone_return=None, fetchall_return=None):
    """psycopg2 연결 MagicMock 생성."""
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fetchone_return
    mock_cur.fetchall.return_value = fetchall_return or []
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    return mock_conn, mock_cur


# ===========================================================================
# 도메인 모델 (dataclass) 테스트
# ===========================================================================

class TestConversationModel:
    def test_basic_fields(self):
        row = _make_conversation_row()
        conv = _row_to_conversation(row)

        assert conv.id == str(row["id"])
        assert conv.owner_id == str(row["owner_id"])
        assert conv.organization_id == str(row["organization_id"])
        assert conv.title == row["title"]
        assert conv.status == "active"
        assert conv.access_level == "private"
        assert conv.deleted_at is None

    def test_soft_delete_field_preserved(self):
        now = datetime.now(timezone.utc)
        row = _make_conversation_row(deleted_at=now, status="deleted")
        conv = _row_to_conversation(row)

        assert conv.deleted_at == now
        assert conv.status == "deleted"

    def test_expires_at_set(self):
        expires = datetime.now(timezone.utc) + timedelta(days=90)
        row = _make_conversation_row(expires_at=expires)
        conv = _row_to_conversation(row)
        assert conv.expires_at == expires

    def test_metadata_defaults_to_empty_dict_when_none(self):
        row = _make_conversation_row(metadata=None)
        conv = _row_to_conversation(row)
        assert conv.metadata == {}


class TestTurnModel:
    def test_basic_fields(self):
        row = _make_turn_row()
        turn = _row_to_turn(row)

        assert turn.id == str(row["id"])
        assert turn.conversation_id == str(row["conversation_id"])
        assert turn.turn_number == 1
        assert turn.user_message == row["user_message"]
        assert turn.assistant_response == row["assistant_response"]

    def test_retrieval_metadata_structure(self):
        metadata = {
            "citations": [{"document_id": str(uuid4()), "content_hash": "abc"}],
            "query_original": "원문 쿼리",
            "query_rewritten": "재작성된 쿼리",
            "context_window_turns": [],
            "retrieval_time_ms": 150,
        }
        row = _make_turn_row(retrieval_metadata=metadata)
        turn = _row_to_turn(row)
        assert turn.retrieval_metadata["query_original"] == "원문 쿼리"
        assert len(turn.retrieval_metadata["citations"]) == 1

    def test_retrieval_metadata_defaults_to_empty_dict(self):
        row = _make_turn_row(retrieval_metadata=None)
        turn = _row_to_turn(row)
        assert turn.retrieval_metadata == {}


class TestMessageModel:
    def test_basic_fields(self):
        row = _make_message_row()
        msg = _row_to_message(row)

        assert msg.id == str(row["id"])
        assert msg.role == "user"
        assert msg.content == row["content"]

    def test_role_values(self):
        for role in ("user", "assistant", "system"):
            row = _make_message_row(role=role)
            msg = _row_to_message(row)
            assert msg.role == role

    def test_metadata_defaults(self):
        row = _make_message_row(metadata=None)
        msg = _row_to_message(row)
        assert msg.metadata == {}


# ===========================================================================
# ConversationRepository 테스트
# ===========================================================================

class TestConversationRepository:
    def test_get_by_id_found(self):
        row = _make_conversation_row()
        conn, cur = _make_mock_conn(fetchone_return=row)
        repo = ConversationRepository(conn)

        result = repo.get_by_id(str(row["id"]))

        assert result is not None
        assert result.id == str(row["id"])
        cur.execute.assert_called_once()
        sql_called = cur.execute.call_args[0][0]
        assert "deleted_at IS NULL" in sql_called

    def test_get_by_id_not_found(self):
        conn, cur = _make_mock_conn(fetchone_return=None)
        repo = ConversationRepository(conn)

        result = repo.get_by_id(str(uuid4()))
        assert result is None

    def test_list_by_owner_returns_paginated(self):
        rows = [_make_conversation_row() for _ in range(3)]
        # count 쿼리 → fetchone, list 쿼리 → fetchall
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = {"count": 3}
        mock_cur.fetchall.return_value = rows
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        repo = ConversationRepository(mock_conn)
        conversations, total = repo.list_by_owner(
            str(uuid4()), str(uuid4()), limit=10, offset=0
        )

        assert total == 3
        assert len(conversations) == 3

    def test_list_by_owner_fts_adds_search_condition(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = {"count": 0}
        mock_cur.fetchall.return_value = []
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        repo = ConversationRepository(mock_conn)
        repo.list_by_owner(
            str(uuid4()), str(uuid4()), search_query="AI 정책"
        )

        # FTS 쿼리 포함 여부 확인
        all_sqls = " ".join(
            str(c[0][0]) for c in mock_cur.execute.call_args_list
        )
        assert "plainto_tsquery" in all_sqls

    def test_create_sets_expires_at(self):
        row = _make_conversation_row()
        conn, cur = _make_mock_conn(fetchone_return=row)
        repo = ConversationRepository(conn)

        result = repo.create(
            owner_id=str(uuid4()),
            organization_id=str(uuid4()),
            title="새 대화",
            retention_days=30,
        )

        assert result is not None
        sql_called = cur.execute.call_args[0][0]
        # expires_at 자동 계산 SQL 포함 여부
        assert "INTERVAL" in sql_called

    def test_soft_delete_marks_deleted(self):
        deleted_row = {"id": uuid4()}
        conn, cur = _make_mock_conn(fetchone_return=deleted_row)
        repo = ConversationRepository(conn)

        deleted = repo.soft_delete(str(uuid4()))

        assert deleted is True
        sql_called = cur.execute.call_args[0][0]
        assert "deleted_at = NOW()" in sql_called
        assert "status = 'deleted'" in sql_called

    def test_soft_delete_returns_false_when_not_found(self):
        conn, cur = _make_mock_conn(fetchone_return=None)
        repo = ConversationRepository(conn)

        deleted = repo.soft_delete(str(uuid4()))
        assert deleted is False

    def test_mark_expired(self):
        conn, cur = _make_mock_conn(fetchone_return={"id": uuid4()})
        repo = ConversationRepository(conn)

        result = repo.mark_expired(str(uuid4()))

        assert result is True
        sql_called = cur.execute.call_args[0][0]
        assert "status = 'expired'" in sql_called


# ===========================================================================
# TurnRepository 테스트
# ===========================================================================

class TestTurnRepository:
    def test_create_turn(self):
        row = _make_turn_row()
        conn, cur = _make_mock_conn(fetchone_return=row)
        repo = TurnRepository(conn)

        turn = repo.create(
            conversation_id=str(uuid4()),
            turn_number=1,
            user_message="질문",
            assistant_response="응답",
            retrieval_metadata={"citations": []},
        )

        assert turn.turn_number == 1
        # retrieval_metadata 는 JSON 직렬화 후 전달
        call_params = cur.execute.call_args[0][1]
        assert json.loads(call_params[4]) == {"citations": []}

    def test_list_by_conversation_asc_order(self):
        rows = [_make_turn_row(turn_number=i) for i in range(1, 4)]
        conn, cur = _make_mock_conn(fetchall_return=rows)
        repo = TurnRepository(conn)

        turns = repo.list_by_conversation(str(uuid4()), order="ASC")

        assert [t.turn_number for t in turns] == [1, 2, 3]
        sql_called = cur.execute.call_args[0][0]
        assert "ASC" in sql_called

    def test_list_by_conversation_desc_reverses(self):
        # DESC로 DB에서 조회 후 Python 에서 reverse → 반환은 오름차순
        rows = [_make_turn_row(turn_number=i) for i in [3, 2, 1]]
        conn, cur = _make_mock_conn(fetchall_return=rows)
        repo = TurnRepository(conn)

        turns = repo.list_by_conversation(str(uuid4()), order="DESC")
        assert [t.turn_number for t in turns] == [1, 2, 3]

    def test_list_by_conversation_with_limit(self):
        rows = [_make_turn_row(turn_number=i) for i in [3, 2]]
        conn, cur = _make_mock_conn(fetchall_return=rows)
        repo = TurnRepository(conn)

        repo.list_by_conversation(str(uuid4()), limit=2, order="DESC")

        sql_called = cur.execute.call_args[0][0]
        assert "LIMIT" in sql_called

    def test_redact_turn_replaces_user_message(self):
        conn, cur = _make_mock_conn(fetchone_return={"id": uuid4()})
        repo = TurnRepository(conn)

        result = repo.redact_turn(str(uuid4()), fields=["user_message"])

        assert result is True
        sql_called = cur.execute.call_args[0][0]
        assert "user_message = '[REDACTED]'" in sql_called

    def test_redact_turn_ignores_unknown_fields(self):
        conn, cur = _make_mock_conn(fetchone_return=None)
        repo = TurnRepository(conn)

        # 허용되지 않은 필드만 전달
        result = repo.redact_turn(str(uuid4()), fields=["unknown_field"])
        assert result is False
        cur.execute.assert_not_called()

    def test_next_turn_number(self):
        conn, cur = _make_mock_conn(fetchone_return={"next_num": 5})
        repo = TurnRepository(conn)

        num = repo.next_turn_number(str(uuid4()))
        assert num == 5


# ===========================================================================
# MessageRepository 테스트
# ===========================================================================

class TestMessageRepository:
    def test_create_message(self):
        row = _make_message_row()
        conn, cur = _make_mock_conn(fetchone_return=row)
        repo = MessageRepository(conn)

        msg = repo.create(
            turn_id=str(uuid4()),
            role="assistant",
            content="응답 내용",
            metadata={"model": "gpt-4o", "token_count": 42},
        )

        assert msg.role == "user"  # row fixture is role=user
        call_params = cur.execute.call_args[0][1]
        assert json.loads(call_params[3]) == {"model": "gpt-4o", "token_count": 42}

    def test_create_bulk_messages(self):
        rows = [_make_message_row(role=r) for r in ("system", "user", "assistant")]
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = rows
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        repo = MessageRepository(mock_conn)
        messages_input = [
            {"role": "system", "content": "You are helpful.", "metadata": {}},
            {"role": "user", "content": "질문", "metadata": {}},
            {"role": "assistant", "content": "응답", "metadata": {}},
        ]
        result = repo.create_bulk(str(uuid4()), messages_input)
        assert len(result) == 3

    def test_create_bulk_empty_list(self):
        conn, cur = _make_mock_conn()
        repo = MessageRepository(conn)

        result = repo.create_bulk(str(uuid4()), [])
        assert result == []
        cur.execute.assert_not_called()

    def test_list_by_turn_ordered(self):
        rows = [_make_message_row() for _ in range(3)]
        conn, cur = _make_mock_conn(fetchall_return=rows)
        repo = MessageRepository(conn)

        messages = repo.list_by_turn(str(uuid4()))
        assert len(messages) == 3
        sql_called = cur.execute.call_args[0][0]
        assert "created_at ASC" in sql_called


# ===========================================================================
# AuditEmitter actor_type 검증
# ===========================================================================

class TestAuditEmitterActorType:
    def test_emit_includes_actor_type_in_log(self):
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()
        with patch.object(emitter, "_persist") as mock_persist:
            emitter.emit(
                event_type="conversation.created",
                action="conversation.create",
                actor_id=str(uuid4()),
                actor_type="user",
                resource_type="conversation",
                resource_id=str(uuid4()),
                result="success",
            )
            mock_persist.assert_called_once()
            kwargs = mock_persist.call_args[1]
            assert kwargs["actor_type"] == "user"

    def test_emit_agent_actor_type(self):
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()
        with patch.object(emitter, "_persist") as mock_persist:
            emitter.emit(
                event_type="conversation.created",
                action="conversation.create",
                actor_id="agent-001",
                actor_type="agent",
                resource_type="conversation",
                resource_id=str(uuid4()),
                result="success",
            )
            kwargs = mock_persist.call_args[1]
            assert kwargs["actor_type"] == "agent"

    def test_emit_for_actor_maps_service_to_agent(self):
        from app.api.auth.models import ActorContext, ActorType
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()
        actor = ActorContext(
            actor_type=ActorType.SERVICE,
            actor_id="svc-001",
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        with patch.object(emitter, "_persist") as mock_persist:
            emitter.emit_for_actor(
                event_type="conversation.deleted",
                action="conversation.delete",
                actor=actor,
                resource_type="conversation",
                resource_id=str(uuid4()),
            )
            kwargs = mock_persist.call_args[1]
            assert kwargs["actor_type"] == "agent"

    def test_emit_for_actor_maps_user_to_user(self):
        from app.api.auth.models import ActorContext, ActorType
        from app.audit.emitter import AuditEmitter

        emitter = AuditEmitter()
        actor = ActorContext(
            actor_type=ActorType.USER,
            actor_id=str(uuid4()),
            is_authenticated=True,
            auth_method=None,
            tenant_id=None,
        )
        with patch.object(emitter, "_persist") as mock_persist:
            emitter.emit_for_actor(
                event_type="conversation.created",
                action="conversation.create",
                actor=actor,
                resource_type="conversation",
                resource_id=str(uuid4()),
            )
            kwargs = mock_persist.call_args[1]
            assert kwargs["actor_type"] == "user"
