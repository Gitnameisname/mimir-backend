"""
Conversation / Turn / Message 저장소 — Phase 3 S2.

책임:
  - conversations / turns / messages 테이블에 대한 SQL CRUD 실행
  - DB row(RealDictRow) → 도메인 모델 변환
  - Scope Profile 기반 ACL 필터링 의무 적용 (S2 원칙 ⑥)
    - 접근 범위 어휘는 Scope Profile 로 관리 — 코드에 scope 문자열 하드코딩 금지
  - 전체 텍스트 검색 (FTS) 지원

설계 원칙:
  - router / service 는 SQL을 직접 작성하지 않는다.
  - 모든 조회 쿼리에 deleted_at IS NULL 필터 기본 적용 (soft delete).
  - SQL injection 방지: 동적 컬럼명은 whitelist 매핑만 허용.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg2.extensions
import psycopg2.extras

from app.models.conversation import Conversation, Message, Turn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sort 필드 whitelist (SQL injection 방지)
# ---------------------------------------------------------------------------
_CONVERSATION_SORT_FIELDS: dict[str, str] = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "title": "title",
    "status": "status",
}


# ---------------------------------------------------------------------------
# Row → 도메인 모델 변환
# ---------------------------------------------------------------------------

def _row_to_conversation(row: dict[str, Any]) -> Conversation:
    return Conversation(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        organization_id=str(row["organization_id"]),
        title=row["title"],
        status=row["status"],
        metadata=row["metadata"] if row["metadata"] is not None else {},
        retention_days=row["retention_days"],
        access_level=row["access_level"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row.get("expires_at"),
        deleted_at=row.get("deleted_at"),
    )


def _row_to_turn(row: dict[str, Any]) -> Turn:
    return Turn(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        turn_number=row["turn_number"],
        user_message=row["user_message"],
        assistant_response=row["assistant_response"],
        retrieval_metadata=row["retrieval_metadata"] if row["retrieval_metadata"] is not None else {},
        created_at=row["created_at"],
    )


def _row_to_message(row: dict[str, Any]) -> Message:
    return Message(
        id=str(row["id"]),
        turn_id=str(row["turn_id"]),
        role=row["role"],
        content=row["content"],
        metadata=row["metadata"] if row["metadata"] is not None else {},
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# ConversationRepository
# ---------------------------------------------------------------------------

class ConversationRepository:
    """Conversation CRUD 및 조회."""

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get_by_id(self, conversation_id: str) -> Optional[Conversation]:
        """ID로 활성 대화 조회 (soft delete 제외)."""
        sql = """
            SELECT * FROM conversations
            WHERE id = %s AND deleted_at IS NULL
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            row = cur.fetchone()
        return _row_to_conversation(row) if row else None

    def list_by_owner(
        self,
        owner_id: str,
        organization_id: str,
        *,
        status: Optional[str] = "active",
        sort_by: str = "created_at",
        sort_desc: bool = True,
        limit: int = 50,
        offset: int = 0,
        search_query: Optional[str] = None,
    ) -> tuple[list[Conversation], int]:
        """소유자별 대화 목록 조회 (페이지네이션 + FTS 선택).

        Args:
            owner_id        : 소유자 UUID 문자열
            organization_id : 조직 UUID 문자열
            status          : 상태 필터 (None이면 전체)
            sort_by         : 정렬 기준 컬럼 (whitelist 검사)
            sort_desc       : 내림차순 여부
            limit           : 최대 반환 건수
            offset          : 건너뛸 건수
            search_query    : FTS 검색어 (None이면 전체)

        Returns:
            (conversations, total_count)
        """
        sort_col = _CONVERSATION_SORT_FIELDS.get(sort_by, "created_at")
        direction = "DESC" if sort_desc else "ASC"

        base_conditions = [
            "owner_id = %s",
            "organization_id = %s",
            "deleted_at IS NULL",
        ]
        params: list[Any] = [owner_id, organization_id]

        if status is not None:
            base_conditions.append("status = %s")
            params.append(status)

        if search_query:
            base_conditions.append(
                "search_vector @@ plainto_tsquery('simple', %s)"
            )
            params.append(search_query)

        where_clause = " AND ".join(base_conditions)

        count_sql = f"SELECT COUNT(*) FROM conversations WHERE {where_clause}"
        list_sql = (
            f"SELECT * FROM conversations WHERE {where_clause}"
            f" ORDER BY {sort_col} {direction}"
            f" LIMIT %s OFFSET %s"
        )

        with self._conn.cursor() as cur:
            cur.execute(count_sql, params)
            total: int = cur.fetchone()["count"]

            cur.execute(list_sql, params + [limit, offset])
            rows = cur.fetchall()

        conversations = [_row_to_conversation(r) for r in rows]
        return conversations, total

    # ------------------------------------------------------------------
    # 생성
    # ------------------------------------------------------------------

    def create(
        self,
        owner_id: str,
        organization_id: str,
        title: str,
        *,
        retention_days: int = 90,
        metadata: Optional[dict[str, Any]] = None,
        access_level: str = "private",
    ) -> Conversation:
        """새 Conversation 생성.

        expires_at = NOW() + retention_days 로 자동 계산.
        """
        sql = """
            INSERT INTO conversations
                (owner_id, organization_id, title, retention_days,
                 expires_at, metadata, access_level)
            VALUES (%s, %s, %s, %s,
                    NOW() + INTERVAL '1 day' * %s,
                    %s, %s)
            RETURNING *
        """
        params = (
            owner_id,
            organization_id,
            title,
            retention_days,
            retention_days,
            json.dumps(metadata or {}),
            access_level,
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_conversation(row)

    # ------------------------------------------------------------------
    # 수정
    # ------------------------------------------------------------------

    def update(
        self,
        conversation_id: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[Conversation]:
        """대화 제목/상태/메타데이터 수정."""
        set_clauses: list[str] = ["updated_at = NOW()"]
        params: list[Any] = []

        if title is not None:
            set_clauses.append("title = %s")
            params.append(title)
        if status is not None:
            set_clauses.append("status = %s")
            params.append(status)
        if metadata is not None:
            set_clauses.append("metadata = %s")
            params.append(json.dumps(metadata))

        if len(set_clauses) == 1:
            # 변경 항목 없음
            return self.get_by_id(conversation_id)

        params.append(conversation_id)
        sql = f"""
            UPDATE conversations
            SET {', '.join(set_clauses)}
            WHERE id = %s AND deleted_at IS NULL
            RETURNING *
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return _row_to_conversation(row) if row else None

    # ------------------------------------------------------------------
    # 삭제 (soft delete)
    # ------------------------------------------------------------------

    def soft_delete(self, conversation_id: str) -> bool:
        """Soft delete: deleted_at 설정."""
        sql = """
            UPDATE conversations
            SET deleted_at = NOW(), status = 'deleted', updated_at = NOW()
            WHERE id = %s AND deleted_at IS NULL
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            return cur.fetchone() is not None

    def hard_delete(self, conversation_id: str) -> bool:
        """물리 삭제 — 관리자 전용."""
        sql = "DELETE FROM conversations WHERE id = %s RETURNING id"
        with self._conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # 만료 관리 (PII 생명주기 — Task 3-3 배치에서 호출)
    # ------------------------------------------------------------------

    def list_expired(self, limit: int = 200) -> list[Conversation]:
        """expires_at 이 현재 시각 이전인 활성 대화 목록."""
        sql = """
            SELECT * FROM conversations
            WHERE expires_at < NOW()
              AND status NOT IN ('expired', 'deleted')
              AND deleted_at IS NULL
            ORDER BY expires_at ASC
            LIMIT %s
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
        return [_row_to_conversation(r) for r in rows]

    def mark_expired(self, conversation_id: str) -> bool:
        """대화 상태를 expired 로 전환."""
        sql = """
            UPDATE conversations
            SET status = 'expired', updated_at = NOW()
            WHERE id = %s AND status NOT IN ('expired', 'deleted')
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# TurnRepository
# ---------------------------------------------------------------------------

class TurnRepository:
    """Turn CRUD 및 조회."""

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def get_by_id(self, turn_id: str) -> Optional[Turn]:
        sql = "SELECT * FROM turns WHERE id = %s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (turn_id,))
            row = cur.fetchone()
        return _row_to_turn(row) if row else None

    def list_by_conversation(
        self,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
        order: str = "ASC",
    ) -> list[Turn]:
        """대화의 턴 목록 조회 (turn_number 순).

        Args:
            limit : 최근 N개 제한 (컨텍스트 윈도우용)
        """
        direction = "DESC" if order.upper() == "DESC" else "ASC"
        sql = f"""
            SELECT * FROM turns
            WHERE conversation_id = %s
            ORDER BY turn_number {direction}
        """
        params: list[Any] = [conversation_id]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        # DESC로 조회했으면 반환 시 오름차순 재정렬
        turns = [_row_to_turn(r) for r in rows]
        if order.upper() == "DESC":
            turns.reverse()
        return turns

    def next_turn_number(self, conversation_id: str) -> int:
        """다음 turn_number 계산 (MAX + 1, 없으면 1)."""
        sql = """
            SELECT COALESCE(MAX(turn_number), 0) + 1 AS next_num
            FROM turns
            WHERE conversation_id = %s
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (conversation_id,))
            return cur.fetchone()["next_num"]

    def create(
        self,
        conversation_id: str,
        turn_number: int,
        user_message: str,
        assistant_response: str,
        retrieval_metadata: Optional[dict[str, Any]] = None,
    ) -> Turn:
        """새 턴 생성."""
        sql = """
            INSERT INTO turns
                (conversation_id, turn_number, user_message,
                 assistant_response, retrieval_metadata)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    conversation_id,
                    turn_number,
                    user_message,
                    assistant_response,
                    json.dumps(retrieval_metadata or {}),
                ),
            )
            row = cur.fetchone()
        return _row_to_turn(row)

    def update_retrieval_metadata(
        self,
        turn_id: str,
        retrieval_metadata: dict[str, Any],
    ) -> bool:
        """검색 메타데이터 갱신 (RAG 처리 완료 후 호출)."""
        sql = """
            UPDATE turns
            SET retrieval_metadata = %s
            WHERE id = %s
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (json.dumps(retrieval_metadata), turn_id))
            return cur.fetchone() is not None

    def redact_turn(self, turn_id: str, fields: list[str]) -> bool:
        """민감 정보 제거 — 지정 필드를 [REDACTED] 로 교체.

        Args:
            turn_id : 대상 Turn UUID
            fields  : 제거할 필드 목록 ("user_message" | "assistant_response")

        Returns:
            True if the turn was found and updated
        """
        allowed = {"user_message", "assistant_response"}
        target_fields = [f for f in fields if f in allowed]
        if not target_fields:
            return False

        set_clauses = [f"{f} = '[REDACTED]'" for f in target_fields]
        sql = f"""
            UPDATE turns
            SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (turn_id,))
            return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# MessageRepository
# ---------------------------------------------------------------------------

class MessageRepository:
    """Message CRUD 및 조회."""

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def list_by_turn(self, turn_id: str) -> list[Message]:
        """턴의 메시지 목록 (created_at 순)."""
        sql = """
            SELECT * FROM messages
            WHERE turn_id = %s
            ORDER BY created_at ASC
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (turn_id,))
            rows = cur.fetchall()
        return [_row_to_message(r) for r in rows]

    def create(
        self,
        turn_id: str,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Message:
        """새 메시지 생성."""
        sql = """
            INSERT INTO messages (turn_id, role, content, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (turn_id, role, content, json.dumps(metadata or {})),
            )
            row = cur.fetchone()
        return _row_to_message(row)

    def create_bulk(self, turn_id: str, messages: list[dict[str, Any]]) -> list[Message]:
        """여러 메시지 일괄 생성.

        Args:
            messages: [{"role": str, "content": str, "metadata": dict}, ...]
        """
        if not messages:
            return []

        sql = """
            INSERT INTO messages (turn_id, role, content, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """
        created: list[Message] = []
        with self._conn.cursor() as cur:
            for msg in messages:
                cur.execute(
                    sql,
                    (
                        turn_id,
                        msg["role"],
                        msg["content"],
                        json.dumps(msg.get("metadata") or {}),
                    ),
                )
                created.append(_row_to_message(cur.fetchone()))
        return created
