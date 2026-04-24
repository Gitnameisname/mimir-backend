"""
S3 Phase 0 / FG 0-3 후속 S4-A — `app.repositories.rag_repository` 유닛 테스트.

커버 대상:
  - _conv_row / _msg_row (citations/context_chunks JSON 문자열 변환)
  - ensure_tables (SAVEPOINT + 실패 시 ROLLBACK 분기)
  - Conversation: create / get / list / delete / touch
  - Message: add / list / get_history_for_llm (reversed order 정렬)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.repositories.rag_repository import (
    _conv_row,
    _msg_row,
    ensure_tables,
    rag_repository,
)

pytestmark = pytest.mark.unit


CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
USER_ID = "user-00000000-0000-0000-0000-000000000001"
MSG_ID = "msg-00000000-0000-0000-0000-000000000001"
_NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _make_conn(*, fetchone_values=None, fetchall_values=None, rowcount=0):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchone = MagicMock(side_effect=list(fetchone_values)) if fetchone_values is not None else MagicMock(return_value=None)
    cur.fetchall = MagicMock(side_effect=list(fetchall_values)) if fetchall_values is not None else MagicMock(return_value=[])
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn, cur


def _conv(**kw):
    base = {
        "id": CONV_ID, "user_id": USER_ID, "title": "대화 1",
        "document_id": None, "created_at": _NOW, "updated_at": _NOW,
    }
    base.update(kw)
    return base


def _msg(**kw):
    base = {
        "id": MSG_ID, "conversation_id": CONV_ID,
        "role": "user", "content": "질문",
        "citations": [], "context_chunks": [],
        "token_used": None, "model": None,
        "created_at": _NOW,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 1) _conv_row / _msg_row
# --------------------------------------------------------------------------- #


class TestRowHelpers:
    def test_conv_row_happy(self):
        out = _conv_row(_conv(document_id="doc-x"))
        assert out["id"] == CONV_ID
        assert out["document_id"] == "doc-x"
        assert out["title"] == "대화 1"

    def test_conv_row_document_none(self):
        out = _conv_row(_conv(document_id=None))
        assert out["document_id"] is None

    def test_msg_row_with_lists(self):
        out = _msg_row(_msg(citations=[{"id": 1}], context_chunks=[{"x": 1}]))
        assert out["citations"] == [{"id": 1}]
        assert out["context_chunks"] == [{"x": 1}]

    def test_msg_row_with_json_strings(self):
        """citations/context_chunks 가 JSON 문자열일 경우 json.loads 수행."""
        out = _msg_row(_msg(
            citations='[{"s": "A"}]',
            context_chunks='[{"c": 2}]',
        ))
        assert out["citations"] == [{"s": "A"}]
        assert out["context_chunks"] == [{"c": 2}]

    def test_msg_row_defaults_empty_lists_when_missing(self):
        row = _msg(citations=None, context_chunks=None)
        out = _msg_row(row)
        assert out["citations"] == []
        assert out["context_chunks"] == []


# --------------------------------------------------------------------------- #
# 2) ensure_tables
# --------------------------------------------------------------------------- #


class TestEnsureTables:
    def test_executes_each_statement_in_savepoint(self):
        conn, cur = _make_conn()
        ensure_tables(conn)
        # 문장 여러개 — SAVEPOINT / 본문 / RELEASE 가 각각 순차 호출됨
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("SAVEPOINT rag_ddl" in s for s in sqls)
        assert any("RELEASE SAVEPOINT rag_ddl" in s for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS rag_conversations" in s for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS rag_messages" in s for s in sqls)
        assert conn.commit.called

    def test_failed_statement_rolls_back_and_continues(self):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        call_log: list[str] = []

        def _exec(sql, params=None):
            call_log.append(sql)
            # 두 번째 SAVEPOINT 이후의 본문에서 실패 유도 — index 를 간단히
            # "CREATE TABLE IF NOT EXISTS rag_messages" 실행 시 예외
            if "CREATE TABLE IF NOT EXISTS rag_messages" in sql:
                raise RuntimeError("permission denied")

        cur.execute = MagicMock(side_effect=_exec)
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.commit = MagicMock()

        # 예외 전파 없음
        ensure_tables(conn)
        # ROLLBACK 호출됨
        assert any("ROLLBACK TO SAVEPOINT rag_ddl" in s for s in call_log)
        # 이후 문장도 계속 실행 (인덱스 생성 등)
        assert any("CREATE INDEX" in s for s in call_log)


# --------------------------------------------------------------------------- #
# 3) Conversation CRUD
# --------------------------------------------------------------------------- #


class TestConversation:
    def test_create_returns_row(self):
        conn, cur = _make_conn(fetchone_values=[_conv(title="t1", document_id="doc-1")])
        result = rag_repository.create_conversation(
            conn, user_id=USER_ID, title="t1", document_id="doc-1",
        )
        assert result["title"] == "t1"
        assert result["document_id"] == "doc-1"
        sql = cur.execute.call_args.args[0]
        assert "INSERT INTO rag_conversations" in sql

    def test_get_conversation_found(self):
        conn, _ = _make_conn(fetchone_values=[_conv()])
        result = rag_repository.get_conversation(conn, CONV_ID, USER_ID)
        assert result is not None
        assert result["id"] == CONV_ID

    def test_get_conversation_missing(self):
        conn, _ = _make_conn(fetchone_values=[None])
        assert rag_repository.get_conversation(conn, CONV_ID, USER_ID) is None

    def test_list_conversations_returns_items_and_total(self):
        conn, cur = _make_conn(
            fetchone_values=[{"cnt": 2}],
            fetchall_values=[[_conv(title="a"), _conv(title="b")]],
        )
        items, total = rag_repository.list_conversations(conn, USER_ID)
        assert total == 2
        assert len(items) == 2
        assert items[0]["title"] == "a"
        # 두 번째 SQL — ORDER BY updated_at DESC
        list_sql = cur.execute.call_args_list[1].args[0]
        assert "ORDER BY updated_at DESC" in list_sql

    def test_list_conversations_pagination_params(self):
        conn, cur = _make_conn(
            fetchone_values=[{"cnt": 0}], fetchall_values=[[]],
        )
        rag_repository.list_conversations(conn, USER_ID, limit=5, offset=20)
        params = cur.execute.call_args_list[1].args[1]
        assert params == (USER_ID, 5, 20)

    def test_delete_conversation_success(self):
        conn, _ = _make_conn(rowcount=1)
        assert rag_repository.delete_conversation(conn, CONV_ID, USER_ID) is True

    def test_delete_conversation_not_found(self):
        conn, _ = _make_conn(rowcount=0)
        assert rag_repository.delete_conversation(conn, CONV_ID, USER_ID) is False

    def test_touch_conversation_with_title(self):
        conn, cur = _make_conn()
        rag_repository.touch_conversation(conn, CONV_ID, title="새 타이틀")
        sql = cur.execute.call_args.args[0]
        assert "title = %s" in sql
        assert "updated_at = NOW()" in sql
        params = cur.execute.call_args.args[1]
        assert params == ("새 타이틀", CONV_ID)

    def test_touch_conversation_without_title(self):
        conn, cur = _make_conn()
        rag_repository.touch_conversation(conn, CONV_ID)
        sql = cur.execute.call_args.args[0]
        assert "title" not in sql   # title UPDATE 절이 없어야 함
        assert "updated_at = NOW()" in sql
        params = cur.execute.call_args.args[1]
        assert params == (CONV_ID,)


# --------------------------------------------------------------------------- #
# 4) Message
# --------------------------------------------------------------------------- #


class TestMessage:
    def test_add_message_serializes_citations_and_context(self):
        conn, cur = _make_conn(fetchone_values=[_msg(
            citations=[{"doc": "d1"}],
            context_chunks=[{"chunk": "c1"}],
            token_used=123, model="gpt-4o-mini",
        )])
        result = rag_repository.add_message(
            conn,
            message_id=MSG_ID, conversation_id=CONV_ID,
            role="assistant", content="답변",
            citations=[{"doc": "d1"}],
            context_chunks=[{"chunk": "c1"}],
            token_used=123, model="gpt-4o-mini",
        )
        assert result["id"] == MSG_ID
        assert result["role"] == "user"  # _msg default is user — 반환값은 row 기반이라 fixture 가 user
        # 파라미터 검증
        params = cur.execute.call_args.args[1]
        # citations 는 4번째 (index 3), JSON 문자열
        assert json.loads(params[4]) == [{"doc": "d1"}]
        assert json.loads(params[5]) == [{"chunk": "c1"}]

    def test_add_message_defaults_empty_citations(self):
        conn, cur = _make_conn(fetchone_values=[_msg()])
        rag_repository.add_message(
            conn,
            message_id=MSG_ID, conversation_id=CONV_ID,
            role="user", content="hi",
        )
        params = cur.execute.call_args.args[1]
        assert json.loads(params[4]) == []
        assert json.loads(params[5]) == []

    def test_list_messages_returns_ordered_by_created_at_asc(self):
        rows = [_msg(id="m1"), _msg(id="m2"), _msg(id="m3")]
        conn, cur = _make_conn(fetchall_values=[rows])
        result = rag_repository.list_messages(conn, CONV_ID)
        assert [m["id"] for m in result] == ["m1", "m2", "m3"]
        sql = cur.execute.call_args.args[0]
        assert "ORDER BY created_at ASC" in sql

    def test_list_messages_limit_parameter(self):
        conn, cur = _make_conn(fetchall_values=[[]])
        rag_repository.list_messages(conn, CONV_ID, limit=10)
        params = cur.execute.call_args.args[1]
        assert params == (CONV_ID, 10)

    def test_get_history_for_llm_reverses_rows(self):
        """DB 는 created_at DESC 로 가져오고, 응답은 시간 순(ASC)."""
        # fetchall 에 DESC 순서로 제공
        rows = [
            {"role": "assistant", "content": "A3"},
            {"role": "user", "content": "Q3"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q2"},
        ]
        conn, cur = _make_conn(fetchall_values=[rows])
        history = rag_repository.get_history_for_llm(conn, CONV_ID, max_turns=2)
        # reversed — 시간 순 (오래된 것부터)
        assert [h["content"] for h in history] == ["Q2", "A2", "Q3", "A3"]
        # LIMIT = max_turns * 2
        params = cur.execute.call_args.args[1]
        assert params == (CONV_ID, 4)

    def test_get_history_for_llm_default_max_turns_10(self):
        conn, cur = _make_conn(fetchall_values=[[]])
        rag_repository.get_history_for_llm(conn, CONV_ID)
        params = cur.execute.call_args.args[1]
        # 기본 max_turns=10 → 10*2
        assert params == (CONV_ID, 20)
