"""FG 0-3 커버리지 보강 — conversation_repository 유닛 테스트 (세션 14-A).

대상: `backend/app/repositories/conversation_repository.py` (579줄)

커버 범위:
  - _row_to_conversation / _row_to_turn / _row_to_message (None metadata 기본값)
  - ConversationRepository
    - get_by_id (found/None)
    - list_by_owner (기본/status/search_query/정렬 whitelist 미존재 → created_at fallback)
    - create (기본/metadata+retention_days)
    - update (변경 항목 없음 → get_by_id 폴백/단일 필드/복수 필드/삭제 상태)
    - soft_delete / hard_delete (True/False)
    - search_conversations (빈 검색어 → []/count+list)
    - list_expired / mark_expired
  - TurnRepository
    - get_by_id / list_by_conversation (ASC / DESC 재정렬)
    - next_turn_number / create / update_retrieval_metadata / redact_turn (허용 필드 0/일부)
  - MessageRepository
    - list_by_turn / create / create_bulk (empty/복수 메시지)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
    TurnRepository,
    _row_to_conversation,
    _row_to_message,
    _row_to_turn,
)


def _mk_cur(fetchone_values=None, fetchall_values=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    if fetchone_values is not None:
        cur.fetchone = MagicMock(side_effect=list(fetchone_values))
    else:
        cur.fetchone = MagicMock(return_value=None)
    if fetchall_values is not None:
        cur.fetchall = MagicMock(side_effect=list(fetchall_values))
    else:
        cur.fetchall = MagicMock(return_value=[])
    return cur


def _mk_conn(cur):
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _mk_conv_row(
    id_="11111111-1111-1111-1111-111111111111",
    metadata=None,
    status="active",
):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "owner_id": "22222222-2222-2222-2222-222222222222",
        "organization_id": "33333333-3333-3333-3333-333333333333",
        "title": "대화 제목",
        "status": status,
        "metadata": metadata,
        "retention_days": 90,
        "access_level": "private",
        "created_at": now,
        "updated_at": now,
        "expires_at": None,
        "deleted_at": None,
    }


def _mk_turn_row(id_="t1", conv_id="c1", turn_number=1, retrieval_metadata=None):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "conversation_id": conv_id,
        "turn_number": turn_number,
        "user_message": "질문",
        "assistant_response": "답변",
        "retrieval_metadata": retrieval_metadata,
        "created_at": now,
    }


def _mk_msg_row(id_="m1", turn_id="t1", role="user", metadata=None):
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    return {
        "id": id_,
        "turn_id": turn_id,
        "role": role,
        "content": "내용",
        "metadata": metadata,
        "created_at": now,
    }


# ---------------------------------------------------------------------------
# 1. Row → 도메인 변환
# ---------------------------------------------------------------------------


def test_row_to_conversation_default_metadata_when_none():
    conv = _row_to_conversation(_mk_conv_row(metadata=None))
    assert conv.metadata == {}


def test_row_to_conversation_preserves_metadata_dict():
    conv = _row_to_conversation(_mk_conv_row(metadata={"k": "v"}))
    assert conv.metadata == {"k": "v"}


def test_row_to_turn_default_retrieval_metadata():
    turn = _row_to_turn(_mk_turn_row(retrieval_metadata=None))
    assert turn.retrieval_metadata == {}


def test_row_to_message_default_metadata():
    msg = _row_to_message(_mk_msg_row(metadata=None))
    assert msg.metadata == {}


# ---------------------------------------------------------------------------
# 2. ConversationRepository.get_by_id
# ---------------------------------------------------------------------------


def test_conv_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    result = repo.get_by_id("11111111-1111-1111-1111-111111111111")
    assert result is not None
    assert result.title == "대화 제목"


def test_conv_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.get_by_id("missing") is None


# ---------------------------------------------------------------------------
# 3. ConversationRepository.list_by_owner
# ---------------------------------------------------------------------------


def test_conv_list_by_owner_basic():
    cur = _mk_cur(
        fetchone_values=[{"count": 2}],
        fetchall_values=[[_mk_conv_row(), _mk_conv_row()]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    convs, total = repo.list_by_owner("user-1", "org-1")
    assert total == 2
    assert len(convs) == 2


def test_conv_list_by_owner_status_none_omits_filter():
    cur = _mk_cur(
        fetchone_values=[{"count": 0}],
        fetchall_values=[[]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    repo.list_by_owner("user-1", "org-1", status=None)
    count_sql = cur.execute.call_args_list[0][0][0]
    assert "status = %s" not in count_sql


def test_conv_list_by_owner_search_query_adds_fts():
    cur = _mk_cur(
        fetchone_values=[{"count": 0}],
        fetchall_values=[[]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    repo.list_by_owner("user-1", "org-1", search_query="키워드")
    count_sql = cur.execute.call_args_list[0][0][0]
    assert "plainto_tsquery" in count_sql


def test_conv_list_by_owner_sort_by_invalid_falls_back():
    cur = _mk_cur(
        fetchone_values=[{"count": 0}],
        fetchall_values=[[]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    repo.list_by_owner("user-1", "org-1", sort_by="malicious; DROP")
    list_sql = cur.execute.call_args_list[1][0][0]
    # whitelist 기본값 created_at 적용
    assert "ORDER BY created_at" in list_sql


def test_conv_list_by_owner_sort_asc():
    cur = _mk_cur(
        fetchone_values=[{"count": 0}],
        fetchall_values=[[]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    repo.list_by_owner(
        "u", "o", sort_by="title", sort_desc=False
    )
    list_sql = cur.execute.call_args_list[1][0][0]
    assert "ORDER BY title ASC" in list_sql


# ---------------------------------------------------------------------------
# 4. ConversationRepository.create
# ---------------------------------------------------------------------------


def test_conv_create_basic():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    conv = repo.create("u", "o", "제목")
    assert conv.title == "대화 제목"
    params = cur.execute.call_args[0][1]
    # retention_days 가 두 번 (컬럼 값 + INTERVAL 곱셈)
    assert 90 in params


def test_conv_create_with_metadata_and_retention():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    repo.create(
        "u", "o", "T",
        retention_days=30,
        metadata={"source": "web"},
        access_level="team",
    )
    params = cur.execute.call_args[0][1]
    assert 30 in params
    # metadata 가 JSON 문자열로
    assert any("source" in p for p in params if isinstance(p, str))
    assert "team" in params


# ---------------------------------------------------------------------------
# 5. ConversationRepository.update
# ---------------------------------------------------------------------------


def test_conv_update_no_fields_returns_get_by_id():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    result = repo.update("c1")
    assert result is not None


def test_conv_update_title_only():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    result = repo.update("c1", title="새 제목")
    assert result is not None
    sql = cur.execute.call_args[0][0]
    assert "title = %s" in sql


def test_conv_update_all_fields():
    cur = _mk_cur(fetchone_values=[_mk_conv_row()])
    repo = ConversationRepository(_mk_conn(cur))
    repo.update("c1", title="제목", status="archived", metadata={"k": "v"})
    sql = cur.execute.call_args[0][0]
    assert "title = %s" in sql
    assert "status = %s" in sql
    assert "metadata = %s" in sql


def test_conv_update_not_found_returns_none():
    # title 지정 → set_clauses 길이 2 → UPDATE 실행 → fetchone None
    cur = _mk_cur(fetchone_values=[None])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.update("c1", title="X") is None


# ---------------------------------------------------------------------------
# 6. ConversationRepository.soft_delete / hard_delete
# ---------------------------------------------------------------------------


def test_conv_soft_delete_true_when_returned():
    cur = _mk_cur(fetchone_values=[{"id": "c1"}])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.soft_delete("c1") is True


def test_conv_soft_delete_false_when_missing():
    cur = _mk_cur(fetchone_values=[None])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.soft_delete("c1") is False


def test_conv_hard_delete_true_when_returned():
    cur = _mk_cur(fetchone_values=[{"id": "c1"}])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.hard_delete("c1") is True


def test_conv_hard_delete_false_when_missing():
    cur = _mk_cur(fetchone_values=[None])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.hard_delete("c1") is False


# ---------------------------------------------------------------------------
# 7. ConversationRepository.search_conversations
# ---------------------------------------------------------------------------


def test_search_conversations_empty_term_returns_empty():
    repo = ConversationRepository(MagicMock())
    convs, total = repo.search_conversations("org-1", "   ")
    assert convs == []
    assert total == 0


def test_search_conversations_returns_results():
    row_with_rank = _mk_conv_row()
    row_with_rank["_rank"] = 0.8
    cur = _mk_cur(
        fetchone_values=[{"count": 1}],
        fetchall_values=[[row_with_rank]],
    )
    repo = ConversationRepository(_mk_conn(cur))
    convs, total = repo.search_conversations("org-1", "키워드", limit=10, offset=0)
    assert total == 1
    assert len(convs) == 1
    # FTS + ILIKE fallback 이 SQL 에 포함
    list_sql = cur.execute.call_args_list[1][0][0]
    assert "ts_rank" in list_sql
    assert "ILIKE" in list_sql


# ---------------------------------------------------------------------------
# 8. ConversationRepository.list_expired / mark_expired
# ---------------------------------------------------------------------------


def test_list_expired_returns_rows():
    cur = _mk_cur(fetchall_values=[[_mk_conv_row()]])
    repo = ConversationRepository(_mk_conn(cur))
    result = repo.list_expired(limit=100)
    assert len(result) == 1


def test_mark_expired_true():
    cur = _mk_cur(fetchone_values=[{"id": "c1"}])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.mark_expired("c1") is True


def test_mark_expired_false_when_already_expired():
    cur = _mk_cur(fetchone_values=[None])
    repo = ConversationRepository(_mk_conn(cur))
    assert repo.mark_expired("c1") is False


# ---------------------------------------------------------------------------
# 9. TurnRepository
# ---------------------------------------------------------------------------


def test_turn_get_by_id_found():
    cur = _mk_cur(fetchone_values=[_mk_turn_row()])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.get_by_id("t1") is not None


def test_turn_get_by_id_not_found():
    cur = _mk_cur(fetchone_values=[None])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.get_by_id("missing") is None


def test_turn_list_by_conversation_asc():
    cur = _mk_cur(
        fetchall_values=[[_mk_turn_row(turn_number=1), _mk_turn_row(turn_number=2)]]
    )
    repo = TurnRepository(_mk_conn(cur))
    result = repo.list_by_conversation("c1", order="ASC")
    assert [t.turn_number for t in result] == [1, 2]


def test_turn_list_by_conversation_desc_reverses_to_asc():
    # DESC 로 조회 후 파이썬 레벨에서 reverse() → 반환은 오름차순
    cur = _mk_cur(
        fetchall_values=[[_mk_turn_row(turn_number=3), _mk_turn_row(turn_number=2)]]
    )
    repo = TurnRepository(_mk_conn(cur))
    result = repo.list_by_conversation("c1", order="DESC", limit=2)
    assert [t.turn_number for t in result] == [2, 3]


def test_turn_next_turn_number():
    cur = _mk_cur(fetchone_values=[{"next_num": 5}])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.next_turn_number("c1") == 5


def test_turn_create():
    cur = _mk_cur(fetchone_values=[_mk_turn_row()])
    repo = TurnRepository(_mk_conn(cur))
    turn = repo.create("c1", 1, "질문", "답변")
    assert turn.turn_number == 1


def test_turn_create_with_retrieval_metadata():
    cur = _mk_cur(fetchone_values=[_mk_turn_row()])
    repo = TurnRepository(_mk_conn(cur))
    repo.create("c1", 1, "q", "a", retrieval_metadata={"chunks": 3})
    params = cur.execute.call_args[0][1]
    assert any("chunks" in p for p in params if isinstance(p, str))


def test_turn_update_retrieval_metadata_true():
    cur = _mk_cur(fetchone_values=[{"id": "t1"}])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.update_retrieval_metadata("t1", {"k": "v"}) is True


def test_turn_update_retrieval_metadata_false():
    cur = _mk_cur(fetchone_values=[None])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.update_retrieval_metadata("t1", {}) is False


def test_turn_redact_turn_no_allowed_fields_returns_false():
    repo = TurnRepository(MagicMock())
    assert repo.redact_turn("t1", ["title", "random_field"]) is False


def test_turn_redact_turn_updates_allowed_fields():
    cur = _mk_cur(fetchone_values=[{"id": "t1"}])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.redact_turn("t1", ["user_message"]) is True
    sql = cur.execute.call_args[0][0]
    assert "user_message = '[REDACTED]'" in sql
    assert "assistant_response" not in sql


def test_turn_redact_turn_returns_false_when_no_row():
    cur = _mk_cur(fetchone_values=[None])
    repo = TurnRepository(_mk_conn(cur))
    assert repo.redact_turn("t1", ["user_message"]) is False


# ---------------------------------------------------------------------------
# 10. MessageRepository
# ---------------------------------------------------------------------------


def test_message_list_by_turn_returns_ordered():
    cur = _mk_cur(
        fetchall_values=[
            [_mk_msg_row(id_="m1"), _mk_msg_row(id_="m2")]
        ]
    )
    repo = MessageRepository(_mk_conn(cur))
    result = repo.list_by_turn("t1")
    assert len(result) == 2


def test_message_create():
    cur = _mk_cur(fetchone_values=[_mk_msg_row()])
    repo = MessageRepository(_mk_conn(cur))
    msg = repo.create("t1", "user", "안녕", metadata={"source": "api"})
    assert msg.role == "user"


def test_message_create_bulk_empty_returns_empty():
    repo = MessageRepository(MagicMock())
    assert repo.create_bulk("t1", []) == []


def test_message_create_bulk_inserts_all():
    cur = _mk_cur(
        fetchone_values=[_mk_msg_row(id_="m1"), _mk_msg_row(id_="m2")]
    )
    repo = MessageRepository(_mk_conn(cur))
    result = repo.create_bulk(
        "t1",
        [
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "답변", "metadata": {"chunks": 2}},
        ],
    )
    assert len(result) == 2
    # execute 2번
    assert cur.execute.call_count == 2
