"""멀티턴 컨텍스트 윈도우 관리 — Task 3-5.

최근 N개 Turn 조회, 토큰 계산, 오버플로우 처리, 쿼리 재작성 통합을 담당한다.

설계 원칙:
  - S2 원칙 ⑥: ACL 필터링은 컨텍스트 윈도우 Turn 조회에도 의무 적용
  - 토큰 계산은 chunking_service 의 tiktoken 기반 함수 재활용
  - 오버플로우 시 가장 오래된 Turn부터 제거 (oldest-first eviction)
  - 외부 LLM 의존 없이 동작 가능 (S2 원칙 ⑦)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import psycopg2.extensions

from app.models.conversation import Turn
from app.repositories.conversation_repository import ConversationRepository, TurnRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 토큰 카운팅 헬퍼 (tiktoken 없으면 근사치 사용)
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    """텍스트의 토큰 수를 추정한다.

    tiktoken 설치 시 정확한 값, 미설치 시 근사치(4자 ≈ 1토큰)를 반환한다.
    """
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# ContextWindowManager
# ---------------------------------------------------------------------------

class ContextWindowManager:
    """멀티턴 대화의 컨텍스트 윈도우 관리.

    Args:
        conn: psycopg2 DB 연결
    """

    DEFAULT_WINDOW_SIZE: int = 3          # 기본 최근 N턴
    MAX_CONTEXT_TOKENS: int = 4000        # 컨텍스트용 최대 토큰
    BUFFER_RATIO: float = 0.9            # 버퍼: 최대 토큰의 90% 사용 권장

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def fetch_context_window(
        self,
        conversation_id: UUID,
        actor_id: str,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> Tuple[List[Turn], int]:
        """Conversation에서 최근 N개 Turn을 조회한다 (ACL 검증 포함).

        Args:
            conversation_id: 대화 ID
            actor_id: 요청자 ID (ACL 검증용, S2 원칙 ⑥)
            window_size: 조회할 최근 턴 수 (기본: 3)

        Returns:
            (최근 N개 Turn 리스트, 누적 토큰 수) 튜플

        Raises:
            ValueError: conversation 이 존재하지 않을 경우
            PermissionError: actor_id 가 conversation 소유자가 아닌 경우
        """
        conv_id_str = str(conversation_id)

        # ACL 검증 (S2 원칙 ⑥)
        conv_repo = ConversationRepository(self._conn)
        conv = conv_repo.get_by_id(conv_id_str)
        if conv is None:
            raise ValueError(f"Conversation not found: {conversation_id}")
        if conv.owner_id != actor_id:
            raise PermissionError(
                f"User {actor_id} does not have access to conversation {conversation_id}"
            )

        # 최근 N개 Turn 조회 (오름차순 — turn_number 순)
        turn_repo = TurnRepository(self._conn)
        turns = turn_repo.list_by_conversation(
            conv_id_str,
            limit=window_size,
            order="DESC",   # DESC 로 가져오면 내부에서 오름차순 재정렬
        )

        total_tokens = self._calculate_context_tokens(turns)
        logger.info(
            "ContextWindowManager.fetch_context_window: "
            "conversation_id=%s turns=%d tokens=%d",
            conversation_id, len(turns), total_tokens,
        )
        return turns, total_tokens

    def build_context_string(self, turns: List[Turn]) -> str:
        """Turn 리스트를 포맷된 컨텍스트 문자열로 변환한다.

        Args:
            turns: Turn 객체 리스트 (turn_number 오름차순)

        Returns:
            포맷된 컨텍스트 문자열
        """
        if not turns:
            return ""

        lines: List[str] = []
        for turn in turns:
            lines.append(f"Turn {turn.turn_number}:")
            lines.append(f"  User: {turn.user_message}")
            lines.append(f"  Assistant: {turn.assistant_response}")
            lines.append("")
        return "\n".join(lines)

    def manage_overflow(
        self,
        conversation_id: UUID,
        actor_id: str,
        query: str,
        search_results: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[List[Turn], List[Dict[str, Any]], Dict[str, Any]]:
        """토큰 오버플로우 상황에서 컨텍스트 윈도우를 동적 조정한다.

        Args:
            conversation_id: 대화 ID
            actor_id: 요청자 ID (ACL 검증)
            query: 현재 쿼리
            search_results: 검색 결과 리스트 (content 키 보유 dict 리스트)
            max_tokens: 최대 토큰 (기본: MAX_CONTEXT_TOKENS * BUFFER_RATIO)

        Returns:
            (조정된 Turn 리스트, 조정된 검색결과, 메타데이터) 튜플
        """
        if search_results is None:
            search_results = []

        effective_max = max_tokens if max_tokens is not None else int(
            self.MAX_CONTEXT_TOKENS * self.BUFFER_RATIO
        )

        # 컨텍스트 윈도우 조회
        context_turns, context_tokens = self.fetch_context_window(
            conversation_id, actor_id, self.DEFAULT_WINDOW_SIZE
        )

        # 필수 토큰 계산
        query_tokens = count_tokens(query)
        search_tokens = self._calculate_search_tokens(search_results)
        overhead = 100  # system prompt / 포맷 오버헤드 추정

        overflow_handled = False

        # 오버플로우 감지 → 오래된 Turn 제거
        while (context_tokens + query_tokens + search_tokens + overhead > effective_max
               and context_turns):
            removed = context_turns.pop(0)
            removed_tokens = count_tokens(removed.user_message) + count_tokens(removed.assistant_response)
            context_tokens = max(0, context_tokens - removed_tokens)
            overflow_handled = True
            logger.warning(
                "ContextWindowManager: overflow — removed turn %d. "
                "remaining context_tokens=%d",
                removed.turn_number, context_tokens,
            )

        # 검색 결과 자르기 (Turn 제거 후에도 부족하면)
        remaining_budget = effective_max - context_tokens - query_tokens - overhead
        adjusted_search = self._trim_search_results(search_results, remaining_budget)

        metadata: Dict[str, Any] = {
            "context_overflow_handled": overflow_handled,
            "context_tokens": context_tokens,
            "query_tokens": query_tokens,
            "search_tokens": self._calculate_search_tokens(adjusted_search),
            "total_tokens": (
                context_tokens + query_tokens
                + self._calculate_search_tokens(adjusted_search) + overhead
            ),
        }
        return context_turns, adjusted_search, metadata

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _calculate_context_tokens(self, turns: List[Turn]) -> int:
        """Turn 리스트의 누적 토큰 수를 반환한다."""
        total = 0
        for turn in turns:
            total += count_tokens(turn.user_message)
            total += count_tokens(turn.assistant_response)
        return total

    def _calculate_search_tokens(self, search_results: List[Dict[str, Any]]) -> int:
        """검색 결과 리스트의 누적 토큰 수를 반환한다."""
        total = 0
        for result in search_results:
            total += count_tokens(result.get("content", ""))
        return total

    def _trim_search_results(
        self,
        search_results: List[Dict[str, Any]],
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        """검색 결과를 토큰 예산에 맞게 앞에서부터 포함한다."""
        if max_tokens <= 0:
            return []
        trimmed: List[Dict[str, Any]] = []
        used = 0
        for result in search_results:
            tokens = count_tokens(result.get("content", ""))
            if used + tokens <= max_tokens:
                trimmed.append(result)
                used += tokens
            else:
                logger.info(
                    "ContextWindowManager: search results trimmed at %d/%d tokens",
                    used, max_tokens,
                )
                break
        return trimmed
