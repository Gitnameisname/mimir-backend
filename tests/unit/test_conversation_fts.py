"""
대화 FTS 검색 단위 테스트 — PH3-CARRY-002 (task7-10)

psycopg2 cursor를 Mock으로 대체하여 DB 없이 테스트.
title_tsv tsvector 컬럼 + GIN 인덱스 + search_conversations() 검증.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.repositories.conversation_repository import ConversationRepository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mock_conn(fetchone_val=None, fetchall_val=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    if fetchone_val is not None:
        cur.fetchone.return_value = fetchone_val
    if fetchall_val is not None:
        cur.fetchall.return_value = fetchall_val
    return conn, cur


_NOW = datetime.now(timezone.utc)


def _make_conv_row(title: str = "테스트 대화", scope_id: str = "scope-1") -> dict:
    return {
        "id": uuid4(),
        "owner_id": uuid4(),
        "organization_id": scope_id,
        "title": title,
        "status": "active",
        "metadata": {},
        "retention_days": 90,
        "access_level": "private",
        "created_at": _NOW,
        "updated_at": _NOW,
        "expires_at": None,
        "deleted_at": None,
        "_rank": 0.1,
    }


# ---------------------------------------------------------------------------
# search_conversations — 정상 경로
# ---------------------------------------------------------------------------

class TestSearchConversations:
    def test_returns_matching_rows(self):
        row = _make_conv_row("도커 컨테이너 설정 가이드")
        conn, cur = _mock_conn(
            fetchone_val={"count": 1},
            fetchall_val=[row],
        )
        repo = ConversationRepository(conn)
        results, total = repo.search_conversations("scope-1", "도커")

        assert total == 1
        assert len(results) == 1
        assert results[0].title == "도커 컨테이너 설정 가이드"

    def test_executes_two_queries(self):
        """COUNT + SELECT 두 쿼리가 실행되어야 한다."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "검색어")
        assert cur.execute.call_count == 2

    def test_sql_uses_plainto_tsquery(self):
        """실행된 SQL에 plainto_tsquery 가 포함되어야 한다."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "도커")

        executed_sqls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("plainto_tsquery" in sql for sql in executed_sqls)

    def test_sql_uses_tsquery_match_operator(self):
        """실행된 SQL에 @@ 연산자가 포함되어야 한다."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "검색어")

        sqls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("@@" in sql for sql in sqls)

    def test_sql_uses_ts_rank(self):
        """SELECT 쿼리에 ts_rank 가 포함되어야 한다."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "검색어")

        sqls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("ts_rank" in sql for sql in sqls)

    def test_sql_includes_ilike_fallback(self):
        """title_tsv IS NULL 인 레코드를 위한 ILIKE 폴백이 포함되어야 한다."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "검색어")

        sqls = [str(call.args[0]) for call in cur.execute.call_args_list]
        assert any("ILIKE" in sql for sql in sqls)

    def test_scope_id_is_always_in_params(self):
        """organization_id = scope_id ACL 필터가 파라미터에 전달되어야 한다 (S2 ⑥)."""
        conn, cur = _mock_conn(
            fetchone_val={"count": 0},
            fetchall_val=[],
        )
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-acl-test", "검색어")

        all_params = [call.args[1] for call in cur.execute.call_args_list]
        assert any("scope-acl-test" in str(p) for p in all_params)

    def test_empty_search_term_returns_empty(self):
        """공백/빈 검색어는 즉시 ([], 0) 반환 — DB 쿼리 없음."""
        conn, cur = _mock_conn()
        repo = ConversationRepository(conn)

        results, total = repo.search_conversations("scope-1", "   ")
        assert results == []
        assert total == 0
        cur.execute.assert_not_called()

    def test_returns_empty_when_no_match(self):
        conn, cur = _mock_conn(fetchone_val={"count": 0}, fetchall_val=[])
        repo = ConversationRepository(conn)
        results, total = repo.search_conversations("scope-1", "존재하지않는검색어xyz")
        assert total == 0
        assert results == []

    def test_pagination_params_forwarded(self):
        """limit / offset 이 쿼리 파라미터로 전달되어야 한다."""
        conn, cur = _mock_conn(fetchone_val={"count": 5}, fetchall_val=[])
        repo = ConversationRepository(conn)
        repo.search_conversations("scope-1", "도커", offset=10, limit=5)

        list_call_params = cur.execute.call_args_list[1].args[1]
        assert 5 in list_call_params   # limit
        assert 10 in list_call_params  # offset

    def test_multiple_results(self):
        rows = [
            _make_conv_row("도커 컨테이너 설정", "s"),
            _make_conv_row("도커 Compose 가이드", "s"),
        ]
        conn, cur = _mock_conn(fetchone_val={"count": 2}, fetchall_val=rows)
        repo = ConversationRepository(conn)
        results, total = repo.search_conversations("s", "도커")
        assert total == 2
        assert len(results) == 2

    def test_korean_english_mixed_query(self):
        """한국어+영어 혼합 검색어도 정상 처리 (simple dictionary)."""
        conn, cur = _mock_conn(fetchone_val={"count": 1}, fetchall_val=[
            _make_conv_row("Docker 컨테이너 배포 방법"),
        ])
        repo = ConversationRepository(conn)
        results, total = repo.search_conversations("scope-1", "Docker 컨테이너")
        assert total == 1
        assert "Docker" in results[0].title


# ---------------------------------------------------------------------------
# DDL 스모크 테스트 — connection.py 에서 title_tsv DDL 이 정의됐는지
# ---------------------------------------------------------------------------

class TestTitleTsvDDLDefined:
    def test_title_tsv_ddl_constant_exists(self):
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "title_tsv" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_ddl_contains_gin_index(self):
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "GIN" in _CONVERSATIONS_TITLE_TSV_DDL
        assert "idx_conv_title_fts" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_ddl_contains_trigger(self):
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "trg_conv_title_tsv" in _CONVERSATIONS_TITLE_TSV_DDL
        assert "update_conv_title_tsv" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_ddl_contains_data_migration(self):
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "UPDATE conversations" in _CONVERSATIONS_TITLE_TSV_DDL
        assert "to_tsvector" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_ddl_uses_simple_dictionary(self):
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "'simple'" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_ddl_is_idempotent(self):
        """ADD COLUMN IF NOT EXISTS 및 CREATE INDEX IF NOT EXISTS 로 멱등성 보장."""
        from app.db.connection import _CONVERSATIONS_TITLE_TSV_DDL
        assert "IF NOT EXISTS" in _CONVERSATIONS_TITLE_TSV_DDL

    def test_title_tsv_ddl_registered_in_init_db(self):
        """init_db 함수가 _CONVERSATIONS_TITLE_TSV_DDL 을 실행하는지 소스 검증."""
        import inspect
        from app.db import connection as conn_mod
        src = inspect.getsource(conn_mod.init_db)
        assert "_CONVERSATIONS_TITLE_TSV_DDL" in src
