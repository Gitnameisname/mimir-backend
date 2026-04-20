"""
RAG 대화 및 메시지 리포지터리.

Phase 11: rag_conversations / rag_messages 테이블 CRUD.

DB 마이그레이션 (초기 실행 시 자동 생성):
  - rag_conversations
  - rag_messages
"""

import json
import logging
from typing import Optional

import psycopg2.extensions
import psycopg2.extras

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 테이블 자동 생성 (개발 편의 — 운영은 마이그레이션 도구 사용)
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS rag_conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    title           TEXT,
    document_id     UUID,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES rag_conversations(id) ON DELETE CASCADE,
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    citations       JSONB DEFAULT '[]'::jsonb,
    context_chunks  JSONB DEFAULT '[]'::jsonb,
    token_used      INTEGER,
    model           VARCHAR(100),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_conversations_user_id
    ON rag_conversations(user_id);

CREATE INDEX IF NOT EXISTS idx_rag_messages_conversation_id
    ON rag_messages(conversation_id);
"""


def ensure_tables(conn: psycopg2.extensions.connection) -> None:
    """rag_conversations / rag_messages 테이블이 없으면 생성한다.

    테이블 소유권이 없어 일부 DDL이 실패해도 나머지를 계속 실행한다.
    """
    statements = [s.strip() for s in _CREATE_TABLES_SQL.split(";") if s.strip()]
    skipped = 0
    with conn.cursor() as cur:
        for stmt in statements:
            try:
                cur.execute("SAVEPOINT rag_ddl")
                cur.execute(stmt)
                cur.execute("RELEASE SAVEPOINT rag_ddl")
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT rag_ddl")
                skipped += 1
                logger.debug("RAG DDL skipped: %s", str(exc).strip())
    conn.commit()
    if skipped:
        logger.warning("RAG 테이블 init: %d 구문 건너뜀 (이미 존재하거나 권한 부족)", skipped)


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------

class RAGRepository:
    """rag_conversations / rag_messages 테이블 접근 레이어."""

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------

    def create_conversation(
        self,
        conn: psycopg2.extensions.connection,
        *,
        user_id: str,
        title: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_conversations (user_id, title, document_id)
                VALUES (%s::uuid, %s, %s::uuid)
                RETURNING id, user_id, title, document_id, created_at, updated_at
                """,
                (user_id, title, document_id),
            )
            row = cur.fetchone()
        return _conv_row(row)

    def get_conversation(
        self,
        conn: psycopg2.extensions.connection,
        conversation_id: str,
        user_id: str,
    ) -> Optional[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, title, document_id, created_at, updated_at
                FROM rag_conversations
                WHERE id = %s::uuid AND user_id = %s::uuid
                """,
                (conversation_id, user_id),
            )
            row = cur.fetchone()
        return _conv_row(row) if row else None

    def list_conversations(
        self,
        conn: psycopg2.extensions.connection,
        user_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM rag_conversations WHERE user_id = %s::uuid",
                (user_id,),
            )
            total = cur.fetchone()["cnt"]

            cur.execute(
                """
                SELECT id, user_id, title, document_id, created_at, updated_at
                FROM rag_conversations
                WHERE user_id = %s::uuid
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
                """,
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
        return [_conv_row(r) for r in rows], total

    def delete_conversation(
        self,
        conn: psycopg2.extensions.connection,
        conversation_id: str,
        user_id: str,
    ) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rag_conversations WHERE id = %s::uuid AND user_id = %s::uuid",
                (conversation_id, user_id),
            )
            return cur.rowcount > 0

    def touch_conversation(
        self,
        conn: psycopg2.extensions.connection,
        conversation_id: str,
        title: Optional[str] = None,
    ) -> None:
        """updated_at 갱신. 타이틀이 없으면 첫 메시지 기반으로 자동 설정."""
        with conn.cursor() as cur:
            if title:
                cur.execute(
                    "UPDATE rag_conversations SET updated_at = NOW(), title = %s WHERE id = %s::uuid",
                    (title, conversation_id),
                )
            else:
                cur.execute(
                    "UPDATE rag_conversations SET updated_at = NOW() WHERE id = %s::uuid",
                    (conversation_id,),
                )

    # ------------------------------------------------------------------
    # Message
    # ------------------------------------------------------------------

    def add_message(
        self,
        conn: psycopg2.extensions.connection,
        *,
        message_id: str,
        conversation_id: str,
        role: str,
        content: str,
        citations: Optional[list] = None,
        context_chunks: Optional[list] = None,
        token_used: Optional[int] = None,
        model: Optional[str] = None,
    ) -> dict:
        cit_json = json.dumps(citations or [], ensure_ascii=False)
        ctx_json = json.dumps(context_chunks or [], ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_messages
                    (id, conversation_id, role, content, citations, context_chunks, token_used, model)
                VALUES
                    (%s::uuid, %s::uuid, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                RETURNING id, conversation_id, role, content, citations,
                          context_chunks, token_used, model, created_at
                """,
                (
                    message_id, conversation_id, role, content,
                    cit_json, ctx_json, token_used, model,
                ),
            )
            row = cur.fetchone()
        return _msg_row(row)

    def list_messages(
        self,
        conn: psycopg2.extensions.connection,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, conversation_id, role, content, citations,
                       context_chunks, token_used, model, created_at
                FROM rag_messages
                WHERE conversation_id = %s::uuid
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (conversation_id, limit),
            )
            rows = cur.fetchall()
        return [_msg_row(r) for r in rows]

    def get_history_for_llm(
        self,
        conn: psycopg2.extensions.connection,
        conversation_id: str,
        *,
        max_turns: int = 10,
    ) -> list[dict]:
        """LLM messages 배열 형식으로 이전 대화 이력 반환."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content
                FROM rag_messages
                WHERE conversation_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (conversation_id, max_turns * 2),
            )
            rows = cur.fetchall()
        # 시간 순서로 재정렬
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return history


# ---------------------------------------------------------------------------
# 내부 유틸
# ---------------------------------------------------------------------------

def _conv_row(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "title": row.get("title"),
        "document_id": str(row["document_id"]) if row.get("document_id") else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _msg_row(row: dict) -> dict:
    citations = row.get("citations") or []
    context_chunks = row.get("context_chunks") or []
    if isinstance(citations, str):
        citations = json.loads(citations)
    if isinstance(context_chunks, str):
        context_chunks = json.loads(context_chunks)
    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]),
        "role": row["role"],
        "content": row["content"],
        "citations": citations,
        "context_chunks": context_chunks,
        "token_used": row.get("token_used"),
        "model": row.get("model"),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 싱글턴
# ---------------------------------------------------------------------------

rag_repository = RAGRepository()
